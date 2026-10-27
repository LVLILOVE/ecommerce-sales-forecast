"""
=============================================================================
API v1 · 历史记录与对比          API-17 ~ API-20  （EP-7）
-----------------------------------------------------------------------------
依据文档 : 《开发技术文档 V1.0》§5.7（历史记录接口）
承载页面 : UX-P09 历史列表 / UX-P10 任务详情 / UX-P11 跨任务对比

🔒 **注册顺序是硬约束（BE-39），不是风格问题**
   `POST /history/compare` 必须**先于** `GET /history/{task_id}` 注册。
   FastAPI 按注册顺序做路径匹配：若 `/history/{task_id}` 在前，
   字符串 `compare` 会被当作 `task_id` 解析，`int` 转换失败 → 报
   `40012 BAD_PARAM`，请求**根本进不到对比逻辑**。
   这是「看起来路由写了、实际永远 400」的经典坑，
   故本文件把三条路由写在一个 router 里、用注释锁死顺序，
   并由 `scripts/verify_ep7.py` 断言顺序不可颠倒。

🔒 **四条接口一律要求登录**（`FR-USR-08` / 技术文档 §5.7 脚注）：
   历史里是别人的预测结果，比结果页更敏感，故用 `CurrentUser`
   而非 `OptionalUserId`。游客没有历史 —— 工作台要显式提示这一点，
   而不是让游客撞 401。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.v1._download import client_ip, export_response
from app.core.deps import CurrentUser, DbSession
from app.schemas import (
    ApiResponse,
    CompareRequest,
    CompareResponse,
    ExportQuery,
    HistoryPage,
    PredictionResultResponse,
    WorkbenchSummary,
)
from app.services import history_service, result_service

router = APIRouter(prefix="/history", tags=["历史记录"])


# =============================================================================
# 🔴 API-18 —— 必须最先注册（BE-39）
# =============================================================================
@router.post("/compare", response_model=ApiResponse[CompareResponse], summary="API-18 跨任务对比")
async def compare_tasks(
    payload: CompareRequest,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[CompareResponse]:
    """跨任务曲线对比（`UX-P11`）。

    🔒 耗时预算 ≤ 3 s（`API-18` 契约）。服务层对每个任务只取 20 条曲线
       且**跳过历史段 CSV 读取**（对比图不需要历史段），实测 4 个任务
       在毫秒级完成。

    ⚠️ 归属校验是「全或全无」：任一 `task_id` 不属于当前用户 → `40301`，
       **不返回部分结果**（`API-18-1` / `NFR-SEC-02`）。
    """
    data = await run_in_threadpool(
        history_service.compare,
        db,
        payload.task_ids,
        user_id=user.id,
        sku_ids=payload.sku_ids,
        warehouse_ids=payload.warehouse_ids,
    )
    return ApiResponse.ok(CompareResponse.model_validate(data))


# =============================================================================
# API-17 历史列表（字面量路径，与 /compare 同理必须在 /{task_id} 之前）
# =============================================================================
@router.get("", response_model=ApiResponse[HistoryPage], summary="API-17 历史任务列表")
async def list_history(
    db: DbSession,
    user: CurrentUser,
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=100, description="每页条数，上限 100"),
    keyword: str | None = Query(None, max_length=64, description="任务名 / 数据集名模糊搜索"),
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    status: str | None = Query(None, description="succeeded / failed"),
) -> ApiResponse[HistoryPage]:
    """历史任务列表（`UX-P09`）。

    🔒 耗时预算 ≤ 1 s（`NFR-PERF-08`）：走 `v_task_summary` 视图 +
       `IX-03` 索引，`ORDER BY created_at DESC` 不产生临时 B 树
       （该索引第三列正是它）。
    """
    data = await run_in_threadpool(
        history_service.list_history,
        db,
        user_id=user.id,
        page=page,
        size=size,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    return ApiResponse.ok(HistoryPage.model_validate(data))


# =============================================================================
# UX-P04 工作台（挂在 /history/dashboard，同样是字面量路径）
# =============================================================================
@router.get("/dashboard", response_model=ApiResponse[WorkbenchSummary], summary="UX-P04 工作台概览")
async def workbench(
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[WorkbenchSummary]:
    """工作台概览（`UX-P04`）。

    📌 **最近任务复用 `API-17` 的列表函数**，不另写查询 ——
       两处口径分叉会让「工作台最近任务」与「历史列表首屏」对不上
       （`DOD-EP7` 的验收点之一）。
    """
    data = await run_in_threadpool(
        history_service.workbench_summary, db, user_id=user.id
    )
    return ApiResponse.ok(WorkbenchSummary.model_validate(data))


# =============================================================================
# 以下为参数化路径，必须放在上面所有字面量路径之后
# =============================================================================
@router.get(
    "/{task_id}/export",
    summary="API-19a 历史记录导出（CSV / Excel）",
    response_class=Response,
    responses={
        200: {
            "content": {
                "text/csv": {"schema": {"type": "string", "format": "binary"}},
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                },
            }
        }
    },
)
async def history_export(
    task_id: int,
    q: Annotated[ExportQuery, Query()],
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> Response:
    """历史记录导出（`FR-HIS-03`）。

    🔒 **列定义与结果页完全一致**（`FR-HIS-03` 原文：「列定义同 `FR-VIS-05`」）。
       故这里**直接复用** `result_service.prepare_export` —— 不另写一套列序，
       否则「当时导出的」和「现在从历史导出的」两份文件列不一样，
       用户拿去做对比时会以为数据变了。

    🔒 **要求登录**（`CurrentUser`）：与历史详情同理，历史里是别人的结果，
       比结果页更敏感；且导出是把数据带走。

    ⚠️ **只允许导出 `succeeded` 的任务**（`prepare_export` 内的 `_require_succeeded`）：
       失败任务没有预测行，导出空文件不如明确报错。

    📌 与 `API-16` 共用 `_download.export_response()`：
       文件名双写（RFC 5987）、不设 `Content-Length`、`no-store`
       这三条细节只有一处实现，不会分叉。
    """
    spec = await run_in_threadpool(
        result_service.prepare_export, db, task_id,
        user_id=user.id, fmt=q.format, ip=client_ip(request),
    )
    # 访问日志据此带上 task_id / user_id（`C-11`）
    request.state.task_id = task_id
    request.state.user_id = user.id
    return export_response(spec, task_id)


@router.get(
    "/{task_id}",
    response_model=ApiResponse[PredictionResultResponse],
    summary="API-19 历史任务详情",
)
async def history_detail(
    task_id: int,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[PredictionResultResponse]:
    """任务详情（`UX-P10`）。

    📌 **成功任务的响应结构与 `API-14` 完全一致**（复用同一 schema），
       天然保证「详情内容与当时结果一致」（`FR-HIS-02`）。

    ⚠️ **失败任务不走这条路的同一分支**：`metrics` 为 null、`series` 为空，
       并额外返回 `error_code` / `error_msg` / `completed_stage`。
       结果页刻意不给半成品（`EP-6` 决定），但历史详情**必须**告诉用户
       「为什么失败、卡在哪一步」—— 这是两个不同的诉求。
    """
    data = await run_in_threadpool(
        history_service.get_detail, db, task_id, user_id=user.id
    )
    return ApiResponse.ok(PredictionResultResponse.model_validate(data))


@router.delete("/{task_id}", response_model=ApiResponse[None], summary="API-20 删除历史任务")
async def delete_history(
    task_id: int,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[None]:
    """软删除任务（`FR-HIS-05`）。

    🔒 **只置 `tasks.is_deleted = 1`**，`predictions` / `metrics` 一律保留
       —— 验收要求「删除后可通过数据库查回，满足可追溯」。
       级联软删关联表反而让「查回」变复杂（`API-20-2`）。

    ⚠️ 幂等：重复删除仍返回成功。用户点两次「确认」却看到「任务不存在」
       会以为第一次没删掉。
    """
    await run_in_threadpool(history_service.soft_delete, db, task_id, user_id=user.id)
    return ApiResponse.ok(None)