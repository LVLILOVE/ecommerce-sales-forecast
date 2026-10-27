"""
=============================================================================
预测任务接口（`API-12` / `API-13`）
-----------------------------------------------------------------------------
版本     : V1.0（EP-5）
依据文档 : 《开发技术文档 V1.0》§5.6（`API-12` / `API-13`）、§2.2（`predictions.py` 文件划分）
           《PRD V1.0》`FR-MOD-07` ~ `FR-MOD-13`、`FR-USR-08`、`NFR-PERF-01`、`NFR-REL-02`
           《UI/UX 设计文档 V1.0》§8.3 `UX-P07`（执行页五阶段 / 设备提示 / 软失败卡）
-----------------------------------------------------------------------------
| 编号 | 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|---|
| `API-12` | POST | `/predictions`                    | 游客可（`OptionalUserId`） | 创建并**同步执行**预测任务 |
| `API-13` | GET  | `/predictions/{task_id}/status`   | 游客可（`OptionalUserId`） | 五阶段进度 / 设备 / 耗时 |

`API-14` ~ `API-16`（结果 / 明细分页 / 导出）属 `EP-6`，挂在本文件同一 router 上，
故本文件头只覆盖 `EP-5` 已实现的两个接口。

⚠️ **为什么 `API-12` / `API-13` 也用 `OptionalUserId`**（延续 `EP-4` 裁定 B）
    技术文档 §5.6 的接口表把二者标为「鉴权 = 是」，但同一份文档 §5 的游客态脚注
    （§5.5 后的 `＊` 段）写的是「`API-06` 与 `API-07` 允许未登录调用，**跑到结果页**；
    但导出（`API-16`）与历史（`API-17`~`API-19`）仍然要求登录」。
    `FR-USR-08` 的原话是「未登录用户可加载示例数据并**看到完整预测结果**」——
    而「看到结果」这条链路必须经过 `API-09` → `API-12` → `API-14`，
    因此这三个接口只要有一个改成强制登录，「游客跑到结果页」即刻断裂。
    三者取交集只有一种自洽解：上传 / 参数 / 预测 / 结果这一条链用 `OptionalUserId`，
    导出与历史用 `CurrentUser`，真实用户之间的隔离由 §4.3 `BE-41` 的归属判定守住。
    已登记为偏离项（见实施方案 §7.6 执行记录）。

🔒 **同步执行语义（`API-12`）**：本接口在 60 秒内必然返回，不存在 `queued`。
   故本文件**不**引入任务队列、不返回 202、不做异步入队 —— 那会让
   「60 s 内必然返回」这条硬约束失去意义，也会把 `API-12-2` 的软失败
   （`code: 0` + `status: failed`）变成一条永远走不到的分支。

🔒 **软失败不得变成 HTTP 错误（`API-12-2`）**：`predict_service.create_and_run`
   已经保证超预算 / 推理异常都返回 `code: 0` 的正常载荷。本文件**只做转发**，
   不 `try/except` 改写状态码 —— 一旦在这里捕获并抛 `HTTPException(504)`，
   前端 `UX-P07` 的失败卡（含三条处置建议）就再也拿不到数据。
   `scripts/verify_ep5_api.py` 用「CPU 预算压到 1 ms」的方式坐实这条。

职责边界（`BE-04`）：本文件只做「取参数 → 调 service → 组响应」。
   任何编排 / 状态机 / 设备回退逻辑都必须留在 `services/predict_service.py`，
   否则接口层会长出第二套事实（本项目原型阶段已踩过「两套数字」）。
=============================================================================
"""
from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.v1._download import export_response
from app.core.deps import CurrentUser, DbSession, OptionalUserId
from app.schemas import (
    ApiResponse,
    CreateTaskRequest,
    CreateTaskResponse,
    DetailQuery,
    ExportQuery,
    Paginated,
    PredictionResultResponse,
    PredictionRow,
    TaskStatusResponse,
)
from app.services import predict_service, result_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/predictions", tags=["predictions"])

# 任务终态常量（与 `services/predict_service.py` 的 `STATUS_*` 同源口径）。
# `tasks.status` 的取值域由数据库 `CHECK` 约束固定为四态，
# 此处只用来挑选给用户看的 `message`，不参与任何业务判定。
_STATUS_SUCCEEDED = "succeeded"


def _client_ip(request: Request) -> str | None:
    """客户端 IP（写审计日志用）。与 `api/v1/auth.py` / `datasets.py` 同一口径：
    不读可伪造的 `X-Forwarded-For`。"""
    return request.client.host if request.client else None


# =============================================================================
# `API-12` 创建预测任务（同步执行）
# =============================================================================
@router.post("", response_model=ApiResponse[CreateTaskResponse], summary="创建预测任务")
async def create_prediction(
    request: Request,
    body: CreateTaskRequest,
    user_id: OptionalUserId,
    db: DbSession,
) -> ApiResponse[CreateTaskResponse]:
    """创建并**同步执行**预测任务（`FR-MOD-07` ~ `FR-MOD-13`）。

    🔒 耗时预算 ≤ 60 s（`NFR-PERF-01`），`EP-5` 本机实测约 20 s。
       超预算**不**返回 502/504，而是 `code: 0` + `data.status = "failed"`
       + `data.error_code = "E_TIMEOUT"` + 三条建议（`API-12-2`）。

    🔒 `granularity` 的取值约束由 `CreateTaskRequest`（`app/schemas/task.py`）
       在解析阶段强制 —— 传 `sku` 等值会走统一参数异常处理器返回 `40012`，
       与 §5.6 错误码表一致。**刻意不在本函数里再判一次**：那会让
       「同一约束两处实现、两处可各自漂移」，正是本项目要避免的形态。

    ⚠️ 推理是 CPU/GPU 密集的**阻塞**调用（数十秒），必须放线程池：
       直接在 `async def` 里同步调用会占住事件循环，期间连 `/health`
       都无法响应 —— 表现为「任务在跑，但整个服务像挂了」。
    """
    payload = await run_in_threadpool(
        predict_service.create_and_run,
        db,
        user_id=user_id,
        dataset_id=body.dataset_id,
        horizon=body.horizon,
        use_holiday_feature=body.use_holiday_feature,
        task_name=body.task_name,
        ip=_client_ip(request),
    )

    # 🔴 写进 `request.state`，访问日志才能按 `task_id` / `user_id` 串起链路
    #    （`C-11`：旧访问日志只有 method/path/status/elapsed 四个字段，
    #    排查「某个 task 为什么慢」要跨两个日志源做字符串匹配）。
    request.state.task_id = payload.get("task_id")
    request.state.user_id = user_id

    # ⚠️ `message` 必须随 `status` 变化：`code` 恒为 0（`API-12-2` 的硬要求），
    #    若 `message` 恒为「预测完成」，软失败时前端会先弹一条成功提示
    #    再跳失败卡 —— 用户看到的是自相矛盾的两个结论。
    ok = payload.get("status") == _STATUS_SUCCEEDED
    message = (
        "预测完成，结果已生成"
        if ok
        else "任务已结束但未成功，请查看 data.error_code 与处理建议"
    )
    return ApiResponse.ok(CreateTaskResponse.model_validate(payload), message=message)


# =============================================================================
# `API-13` 查询任务状态
# =============================================================================
@router.get(
    "/{task_id}/status",
    response_model=ApiResponse[TaskStatusResponse],
    summary="查询任务状态",
)
async def task_status(
    task_id: int,
    user_id: OptionalUserId,
    db: DbSession,
) -> ApiResponse[TaskStatusResponse]:
    """执行页 `UX-P07` 的五阶段进度、设备与耗时（§5.6 `API-13`）。

    📌 同步模式下前端**不需要**轮询：`API-12` 返回时任务已入终态。
       本接口保留是为两件事：
         ① 页面刷新后恢复进度显示（内存态丢失时由 DB 降级推断，见 `_from_db`）；
         ② 将来任务变重时平滑演进为真异步，前端无需改造。

    ⚠️ `API-13-1`：`inference` 阶段真实耗时仅约 21 ms（`outputs/eval_report.json`），
       进度条会在该阶段「瞬间跳过」。**这不是缺陷，也不在这里补偿** ——
       平滑动效由前端按 `estimated_ms` 做时间轴插值实现（`UX-GOV-07`）：
       真实耗时优先，不得为了好看而伪造阶段回调。
    """
    payload = await run_in_threadpool(
        predict_service.get_status, db, task_id=task_id, user_id=user_id
    )
    return ApiResponse.ok(TaskStatusResponse.model_validate(payload))


# =============================================================================
# `API-14` 获取预测结果
# =============================================================================
@router.get(
    "/{task_id}",
    response_model=ApiResponse[PredictionResultResponse],
    summary="获取预测结果",
)
async def prediction_result(
    task_id: int,
    user_id: OptionalUserId,
    db: DbSession,
    with_series: bool = True,
    series_limit: int = Query(50, ge=1, le=200, description="内联曲线最大条数"),
    level: str | None = Query(None, pattern="^(L1|L2|L3)$", description="按分级过滤曲线"),
) -> ApiResponse[PredictionResultResponse]:
    """结果页（`UX-P08`）的全部数据：结论 + 指标 + 覆盖率 + 曲线 + 分布。

    🔒 **本接口不做任何指标重算**，全部取自 `EP-5` 落库的
       `metrics` / `predictions` 表（理由见 `services/result_service.py` 模块头）。

    ⚠️ 用 `OptionalUserId` 而非强制登录：`FR-USR-08` 要求游客链路
       能跑到结果页，而「看到结果」必经本接口。归属隔离由
       `result_service._owned_task` 守住（越权 → `40301`，不存在 → `40401`）。
    """
    payload = await run_in_threadpool(
        result_service.get_result,
        db,
        task_id,
        user_id=user_id,
        with_series=with_series,
        series_limit=series_limit,
        level=level,
    )
    return ApiResponse.ok(PredictionResultResponse.model_validate(payload))


# =============================================================================
# `API-15` 结果明细分页
# =============================================================================
@router.get(
    "/{task_id}/detail",
    response_model=ApiResponse[Paginated[PredictionRow]],
    summary="结果明细分页",
)
async def prediction_detail(
    task_id: int,
    q: Annotated[DetailQuery, Query()],
    user_id: OptionalUserId,
    db: DbSession,
) -> ApiResponse[Paginated[PredictionRow]]:
    """结果页明细表：排序 + 分页 + 过滤（`FR-VIS-03`，默认每页 20 行）。

    🔒 **不并入 `API-14`**：单响应若带全部明细，1,000 序列 × 30 天
       = 30,000 行，响应体可达数十 MB，移动端与弱网直接超时。
    """
    payload = await run_in_threadpool(
        result_service.list_detail,
        db,
        task_id,
        user_id=user_id,
        page=q.page,
        size=q.size,
        sort_by=q.sort_by,
        order=q.order,
        level=q.level,
        keyword=q.keyword,
    )
    return ApiResponse.ok(Paginated[PredictionRow].model_validate(payload))


# =============================================================================
# `API-16` 导出预测结果
# =============================================================================
@router.get(
    "/{task_id}/export",
    summary="导出预测结果（CSV / Excel）",
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
async def prediction_export(
    task_id: int,
    q: Annotated[ExportQuery, Query()],
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> Response:
    """导出 CSV / xlsx（`FR-VIS-05` / `UX-P08-08`）。

    🔒 **导出要求登录**（`CurrentUser`，与 `API-14`/`API-15` 不同）：
       `FR-USR-08` 允许游客**看**结果，但数据导出是把数据带走，必须鉴权。
       这是同一份技术文档 §5 两处表述的唯一自洽解（见模块头注释）。

    🔒 **文件名双写**（`API-16-3`）：
       `filename*=UTF-8''<percent-encoded>` 是 RFC 5987 标准写法，中文名在
       各浏览器下都不会乱码；同时保留 ASCII 兜底 `filename="..."`，
       供不认识 `filename*` 的老客户端兜底（它会退化成 `task_42_export.csv`，
       而不是拿到一个乱码名或直接 404）。

    🔒 **流式返回**（2026-10-26 修复）：不再把整份内容读进内存再 `Response(content=)`，
       而是交给 `StreamingResponse` 消费生成器 —— 上限 300 万行时内存恒定。
       因此**不设 `Content-Length`**（长度在生成完毕前未知），由 chunked 传输，
       `Content-Disposition` 依然足以触发浏览器下载。
    """
    spec = await run_in_threadpool(
        result_service.prepare_export, db, task_id,
        user_id=user.id, fmt=q.format, ip=_client_ip(request),
    )
    # 🔴 同上：让访问日志带上 `task_id` / `user_id`（`C-11`）
    request.state.task_id = task_id
    request.state.user_id = user.id
    # 📌 响应头构造抽到 `_download.export_response()`，与历史导出（`API-19a`）共用 ——
    #    文件名双写 / 不设 Content-Length / no-store 三条细节只有一处实现。
    return export_response(spec, task_id)
