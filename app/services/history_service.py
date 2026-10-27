"""历史记录服务（`API-17` ~ `API-20`，`UX-P09` ~ `UX-P11`）。

🔒 **四条接口共用一套归属判定**，不允许各自实现：
   `result_service._owned_task`（`BE-41` 两分支顺序）是唯一真源。
   本模块一律调用它，不重新写「查任务 + 比 user_id」。

🔒 **不重算任何指标**。历史页展示的每个数字都来自
   `metrics` / `predictions` 表或 `v_task_summary` 视图 ——
   与 `EP-6` 的 `API-14` 同源（`GIT-7`）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.deps import GUEST_USER_ID
from app.core.errors import BizError, ErrorCode
from app.dao import event_dao, task_dao
from app.models.event_names import EV_HIS_01_HISTORY_VIEW, EV_HIS_02_HISTORY_COMPARE
from app.services import result_service

logger = logging.getLogger(__name__)

__all__ = [
    "list_history",
    "get_detail",
    "compare",
    "soft_delete",
    "workbench_summary",
    "COMPARE_MIN",
    "COMPARE_MAX",
]

# 🔒 对比任务数上下限（`API-18` 契约：长度 2–4，不重复）。
#    上限 4 是**产品决策**（`UX-P11` 并排 4 列在 1280px 下才不挤），
#    不是技术限制 —— 放宽它前端表格会溢出。
COMPARE_MIN = 2
COMPARE_MAX = 4


# =============================================================================
# `API-17` 历史列表
# =============================================================================
def list_history(
    db: Session,
    *,
    user_id: int = GUEST_USER_ID,
    page: int = 1,
    size: int = 20,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """历史列表（`UX-P09`）。

    🔒 耗时预算 ≤ 1 s（`NFR-PERF-08`）。实测首屏 20 条在 SQLite 上
       是毫秒级 —— 瓶颈从来不是查询，而是**别在列表里画曲线**。

    ⚠️ **失败任务也在列表里**（`API-17-3`）：只过滤 `status` 时才筛掉它。
       用户需要看到「上周那次预测失败了」并能原参数重试，
       把失败任务藏起来等于让用户以为从没跑过。
    """
    if status is not None and status not in ("succeeded", "failed", "running", "created"):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"status 只能是 succeeded / failed / running / created，收到 {status!r}",
            {"status": status},
        )

    rows = task_dao.list_history(
        db,
        user_id,
        page=page,
        size=size,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    total = task_dao.count_history(
        db,
        user_id,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    result = {
        "items": [_history_item(r) for r in rows],
        "page": page,
        "size": size,
        "total": total,
        "pages": (total + size - 1) // size if size else 0,
    }
    # `EV-HIS-01`（`FR-ADM-01`）：历史列表访问 —— 「复购行为」的代理指标
    #（用户回来看自己上次的预测 = 他还在用这个产品）。
    event_dao.record_safe(
        db, event_name=EV_HIS_01_HISTORY_VIEW, user_id=user_id,
        properties={"user_id": user_id, "record_count": len(result["items"])},
    )
    db.commit()
    return result


def _history_item(row: dict[str, Any]) -> dict[str, Any]:
    """视图行 → `HistoryItem`。

    ⚠️ `task_name` 可能为 `NULL`（`tasks.task_name` 允许空）。
       列表上不能显示成空白 —— 那看起来像加载失败。统一回落到
       「任务 #<id>」。⚠️ 但**不能用它替代重跑入口的原始参数**，
       那里要的是 `dataset_id` / `horizon` 等真实字段，不是展示名。
    """
    return {
        "task_id": row["task_id"],
        "task_name": row["task_name"] or f"任务 #{row['task_id']}",
        "status": row["status"],
        "granularity": row["granularity"],
        "horizon": int(row["horizon"] or 0),
        "dataset_id": row.get("dataset_id"),
        "dataset_name": row["dataset_name"] or "（数据集已删除）",
        "series_total": int(row["series_total"] or 0),
        "duration_ms": int(row["duration_ms"] or 0),
        "device": row["device"] or "cpu",
        "device_fallback": int(row["device_fallback"] or 0),
        "model_version": row["model_version"] or "",
        "weighted_mape": result_service._num(row["weighted_mape"]),
        "weighted_mae": result_service._num(row["weighted_mae"]),
        "weighted_rmse": result_service._num(row["weighted_rmse"]),
        "mae_improve_pct": result_service._num(row["mae_improve_pct"]),
        "rmse_improve_pct": result_service._num(row["rmse_improve_pct"]),
        "series_pass_rate": result_service._num(row["series_pass_rate"]),
        "is_over_threshold": bool(row["is_over_threshold"]),
        "error_code": row["error_code"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


# =============================================================================
# `API-19` 历史详情
# =============================================================================
def get_detail(db: Session, task_id: int, *, user_id: int = GUEST_USER_ID) -> dict[str, Any]:
    """任务详情（`UX-P10`）。

    📌 **成功任务的响应结构与 `API-14` 完全一致**（复用
       `PredictionResultResponse`）：`FR-HIS-02` 要求「详情内容与当时
       结果一致」，同一结构天然保证这一点，前端也能复用 `result/` 组件。

    ⚠️ **失败任务走另一条分支**（`API-19` 差异表）：结构相同但
       `metrics` 各字段为 `null`、`series` 为空，外加 `completed_stage`
       与 `error_msg`。不能直接复用 `get_result` —— 它对非成功任务
       抛 `40401`（`EP-6` 的决定：结果页不展示半成品）。
       **两者的区别是有意的**：结果页不该给半成品，但历史详情页
       必须告诉用户「这次为什么失败、卡在哪一步」——
       这是两个不同的用户诉求。
    """
    task = result_service._owned_task(db, task_id, user_id)

    if task.status != "succeeded":
        return _failed_detail(db, task)
    # 成功任务：与结果页同源，逐字段一致
    return result_service.get_result(db, task_id, user_id=user_id)


def _failed_detail(db: Session, task: Any) -> dict[str, Any]:
    """失败 / 未完成任务的详情（`API-19-1`）。"""
    metric = None
    try:
        from app.dao import metric_dao

        metric = metric_dao.get_by_task(db, task.id)
    except Exception:  # noqa: BLE001 - 失败详情不该因指标读不出而 500
        logger.warning("读失败任务指标出错（task=%s）", task.id, exc_info=True)

    return {
        "task": {
            **result_service._task_brief(task),
            "error_code": task.error_code,
            "error_msg": task.error_msg,
            "completed_stage": task.completed_stage,
        },
        "dataset": result_service._dataset_brief(
            result_service._dataset(db, task.dataset_id)
        ),
        # 🔒 指标全为 null 是**契约**（`API-19` 差异表），不是异常。
        #    前端据此渲染「未产出指标」而非「指标为 0」。
        "metrics": result_service._metric_bundle(metric) if metric is not None else _empty_metrics(),
        "coverage": _partial_coverage(task),
        "distribution": None,
        # 🔒 失败任务可能连 `metrics` 行都没有（分级阶段就挂了）。
        #    此时 `timing_ms` 若返回 `{}`，pydantic 会因 5 个必填字段
        #    缺失抛 ValidationError → **历史详情页整页 500**。
        #    满零值是正确答案：没跑过的阶段确实是 0 ms，
        #    而前端只用它渲染「阶段耗时」区块，不参与任何判定。
        "timing_ms": (
            result_service._timing_ms(metric, task)
            if metric is not None
            else {
                "preprocess_ms": 0,
                "features_ms": 0,
                "inference_ms": 0,
                "metrics_ms": 0,
                "total_ms": int(task.duration_ms or 0),
            }
        ),
        "is_over_threshold": False,
        "eval_note": metric.eval_note if metric is not None else "",
        "detail_total": 0,
        "series": [],
        "truncated": False,
    }


def _empty_metrics() -> dict[str, Any]:
    """失败任务的空指标包（字段与 `MetricBundle` 逐一对应，不能少）。"""
    return {
        "weighted_mape": None,
        "weighted_mae": None,
        "weighted_rmse": None,
        "baseline_mape": None,
        "baseline_mae": None,
        "baseline_rmse": None,
        "mae_improve_pct": None,
        "rmse_improve_pct": None,
        "series_evaluated": 0,
        "series_passed": 0,
        "series_pass_rate": None,
        "l1_mape": None,
        "l2_mape": None,
    }


def _partial_coverage(task: Any) -> dict[str, Any]:
    """失败任务的覆盖率（可能部分有效，取决于卡在哪个阶段）。"""
    l1 = int(task.l1_count or 0)
    l2 = int(task.l2_count or 0)
    l3 = int(task.l3_count or 0)
    total = l1 + l2 + l3
    # ⚠️ 失败任务 `series_total` 可能为 NULL（分级阶段就挂了），
    #    此时 total = 0 是**正确**的 —— 「连分级都没跑完」就是 0 条已知，
    #    拿 series_total 去填反而会显示一个没统计过的数字。
    return {
        "L1": l1,
        "L2": l2,
        "L3": l3,
        "total": total,
        "L1_pct": _pct(l1, total),
        "L2_pct": _pct(l2, total),
        "L3_pct": _pct(l3, total),
        "predictable": l1 + l2,
        "predictable_pct": _pct(l1 + l2, total),
    }


def _pct(part: int, total: int) -> float:
    return round(part / total * 100, 2) if total else 0.0


# =============================================================================
# `API-18` 跨任务对比
# =============================================================================
def compare(
    db: Session,
    task_ids: list[int],
    *,
    user_id: int = GUEST_USER_ID,
    sku_ids: list[str] | None = None,
    warehouse_ids: list[str] | None = None,
) -> dict[str, Any]:
    """跨任务曲线对比（`UX-P11`）。

    🔒 `API-18-1`：**所有 `task_ids` 都必须属于当前用户**，任一不属于
       就整体 `40301`，**不得部分返回**。返回一部分再报错的体验最糟 ——
       用户已经看到对比表了，却要自己分辨哪几条是别人的。

    ⚠️ `API-18-2`：**不同 `horizon` 的指标不可直接比较**。7 天步长
       天然比 14 天准（预测越远误差越大），把两者的 MAPE 并排展示
       会让用户得出「短步长更好」的正确结论但**错误的因果**——
       所以必须显式标注「步长不一致，指标不可直接比较」。
    """
    ids = _validate_compare_ids(task_ids)
    _check_sku_limit(sku_ids, 50, "sku_ids")
    _check_sku_limit(warehouse_ids, 10, "warehouse_ids")

    # 🔒 先全量校验归属，再取任何数据（`API-18-1` 的「全或全无」）
    tasks = []
    for tid in ids:
        try:
            tasks.append(result_service._owned_task(db, tid, user_id))
        except BizError as exc:
            raise BizError(
                ErrorCode.FORBIDDEN,
                f"任务 {tid} 不存在或不属于当前账号，"
                f"对比未执行。{exc.message}",
                {"task_ids": ids, "rejected": tid},
            ) from exc

    usable = [t for t in tasks if t.status == "succeeded"]
    warnings = _compare_warnings(tasks, usable)

    payload_tasks = [_compare_task(db, t, sku_ids, warehouse_ids, warnings) for t in tasks]
    result = {
        "tasks": payload_tasks,
        "attribution": _attribution(usable, warnings),
        # 🔒 `API-18-3`：跨数据集对比允许，但结论参考性下降，
        #    前端据此显示显著警告。
        "dataset_consistent": _dataset_consistent(tasks),
        "overlap_dates": _overlap_dates(db, usable),
        "warnings": warnings,
    }
    # `EV-HIS-02`（`FR-ADM-01`）：跨任务对比 —— 对比功能价值验证
    event_dao.record_safe(
        db, event_name=EV_HIS_02_HISTORY_COMPARE, user_id=user_id,
        properties={"task_ids": ids},
    )
    db.commit()
    return result


def _validate_compare_ids(task_ids: list[int]) -> list[int]:
    """校验对比任务集合（长度 2–4、不重复、全为正整数）。"""
    if not isinstance(task_ids, list) or not task_ids:
        raise BizError(
            ErrorCode.BAD_PARAM,
            "task_ids 必填且必须为数组",
            {"task_ids": task_ids},
        )
    if len(task_ids) < COMPARE_MIN or len(task_ids) > COMPARE_MAX:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"对比任务数须为 {COMPARE_MIN}~{COMPARE_MAX} 个，收到 {len(task_ids)} 个",
            {"task_ids": task_ids, "min": COMPARE_MIN, "max": COMPARE_MAX},
        )
    if any(not isinstance(i, int) or i <= 0 for i in task_ids):
        raise BizError(
            ErrorCode.BAD_PARAM,
            "task_ids 必须全部为正整数",
            {"task_ids": task_ids},
        )
    # 🔒 去重**先于**长度校验之外还要保留顺序：用户选的顺序就是
    #    「基准任务 → 对照任务」的顺序，归因以第一个为基准。
    if len(set(task_ids)) != len(task_ids):
        raise BizError(
            ErrorCode.BAD_PARAM,
            "task_ids 中存在重复任务，请勿重复选择同一任务",
            {"task_ids": task_ids},
        )
    return list(task_ids)


def _check_sku_limit(values: list[str] | None, limit: int, name: str) -> None:
    if values and len(values) > limit:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"{name} 最多 {limit} 个，收到 {len(values)} 个。"
            f"序列过多会让对比图糊成一片，请收窄范围",
            {"field": name, "limit": limit, "received": len(values)},
        )


def _compare_task(
    db: Session,
    task: Any,
    sku_ids: list[str] | None,
    warehouse_ids: list[str] | None,
    warnings: list[str],
) -> dict[str, Any]:
    """单个任务的对比载荷。"""
    own: list[str] = []
    if task.status != "succeeded":
        own.append(f"任务 #{task.id} 状态为「{task.status}」，未参与曲线对比")
        return {
            "task_id": task.id,
            "task_name": task.task_name or f"任务 #{task.id}",
            "horizon": int(task.horizon or 0),
            "dataset_id": task.dataset_id,
            "dataset_name": _dataset_name(db, task.dataset_id),
            "created_at": task.created_at,
            "status": task.status,
            "duration_ms": int(task.duration_ms or 0),
            "metrics": None,
            "coverage": None,
            "series": [],
            "warnings": own + _series_scope_warning(sku_ids, warehouse_ids),
        }

    series = _compare_series(db, task, sku_ids, warehouse_ids)
    metric = None
    try:
        from app.dao import metric_dao

        metric = metric_dao.get_by_task(db, task.id)
    except Exception:  # noqa: BLE001
        logger.warning("读对比任务指标出错（task=%s）", task.id, exc_info=True)

    return {
        "task_id": task.id,
        "task_name": task.task_name or f"任务 #{task.id}",
        "horizon": int(task.horizon or 0),
        "dataset_id": task.dataset_id,
        "dataset_name": _dataset_name(db, task.dataset_id),
        "created_at": task.created_at,
        "status": task.status,
        "duration_ms": int(task.duration_ms or 0),
        "metrics": result_service._metric_bundle(metric) if metric is not None else _empty_metrics(),
        "coverage": _partial_coverage(task),
        "series": series,
        "warnings": own + _series_scope_warning(sku_ids, warehouse_ids),
    }


def _series_scope_warning(
    sku_ids: list[str] | None, warehouse_ids: list[str] | None
) -> list[str]:
    if sku_ids or warehouse_ids:
        return ["曲线已按指定商品 / 仓库收窄，仅覆盖筛选范围内的序列"]
    return []


def _compare_series(
    db: Session,
    task: Any,
    sku_ids: list[str] | None,
    warehouse_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """对比用的曲线。

    🔒 对比图只画**预测值**，不画历史段与基线：多个任务叠在一起时，
       历史段会完全重合成一团，基线又几乎与预测重合，4 条线挤在
       一起谁也看不清。对比页要回答的是「不同步长 / 不同模型
       预测得差多少」，历史段不参与这个问题。
    """
    # `API-18` 契约：`sku_ids` 为空则取前 20 条序列（不是 50）
    limit = 20
    curves, _ = result_service._series_curves(
        db, task, limit=limit, level=None, predicted_only=True
    )
    out = []
    for c in curves:
        key = c["key"]
        sku, _, wh = key.partition("-")
        if sku_ids and sku not in sku_ids:
            continue
        if warehouse_ids and wh not in warehouse_ids:
            continue
        out.append(
            {
                "key": key,
                "granularity_level": c["granularity_level"],
                "forecast_dates": c["forecast_dates"],
                "predicted": c["predicted"],
            }
        )
        if len(out) >= limit:
            break
    return out


def _compare_warnings(tasks: list[Any], usable: list[Any]) -> list[str]:
    """跨任务一致性警告（`API-18-2` / `API-18-3`）。"""
    out: list[str] = []
    horizons = sorted({int(t.horizon or 0) for t in usable})
    if len(horizons) > 1:
        out.append(
            f"所选任务的预测步长不一致（{' / '.join(str(h) for h in horizons)} 天），"
            "指标不可直接比较：步长越短预测越准，差异不代表模型优劣。"
            "曲线已只展示时间重叠区间"
        )
    datasets = {t.dataset_id for t in tasks}
    if len(datasets) > 1:
        out.append(
            "所选任务使用了不同的数据文件，指标差异可能来自数据本身而非模型，"
            "结论仅供参考"
        )
    failed = [t for t in tasks if t.status != "succeeded"]
    if failed:
        out.append(
            f"有 {len(failed)} 个任务未成功完成（"
            + "、".join(f"#{t.id} {t.status}" for t in failed)
            + "），其曲线为空"
        )
    return out


def _dataset_consistent(tasks: list[Any]) -> bool:
    return len({t.dataset_id for t in tasks}) <= 1


def _dataset_name(db: Session, dataset_id: int) -> str:
    try:
        ds = result_service._dataset(db, dataset_id)
        return ds.file_name or f"数据集 #{dataset_id}"
    except BizError:  # pragma: no cover - 外键 RESTRICT
        return f"数据集 #{dataset_id}"


def _overlap_dates(db: Session, usable: list[Any]) -> list[str]:
    """所有成功任务预测日期的交集（`API-18-2` 的 ②）。

    🔒 交集为空时返回 `[]`，前端据此**只画指标表不画曲线** ——
       强行按索引对齐会画出「8 月的预测对着 11 月的真值」的假对比。

    ⚠️ 每个任务只取**第一条**序列的日期做交集：对比页的曲线本身就是
       「同一条序列在各任务下的预测」，日期轴取第一条序列已足够判断
       是否重叠。要对全部序列求交集会因个别短序列而误判为「不重叠」。
    """
    sets: list[set[str]] = []
    for t in usable:
        curves, _ = result_service._series_curves(
            db, t, limit=1, level=None, predicted_only=True
        )
        if not curves:
            continue
        dates = set(curves[0]["forecast_dates"])
        if dates:
            sets.append(dates)
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def _attribution(usable: list[Any], warnings: list[str]) -> list[dict[str, Any]]:
    """差异归因（以列表中第一个成功任务为基准，`API-18` 响应字段）。"""
    if len(usable) < 2:
        return []
    base = usable[0]
    out: list[dict[str, Any]] = []
    for t in usable[1:]:
        bits = []
        if int(t.horizon or 0) != int(base.horizon or 0):
            bits.append(
                f"步长由 {base.horizon} 天改为 {t.horizon} 天，指标本身不可直接比较"
            )
        if t.dataset_id != base.dataset_id:
            bits.append(f"数据文件不同（{base.dataset_id} → {t.dataset_id}）")
        if t.model_version != base.model_version:
            bits.append(f"模型版本由 {base.model_version} 改为 {t.model_version}")
        if (t.device or "cpu") != (base.device or "cpu"):
            bits.append(f"推理设备由 {base.device} 改为 {t.device}")
        if not bits:
            bits.append("步长、模型、数据与设备均相同，指标差异来自随机性")
        out.append({"task_id": t.id, "note": "；".join(bits)})
    if warnings:
        out.append(
            {
                "task_id": base.id,
                "note": "（基准任务）" + "；".join(warnings),
            }
        )
    return out


# =============================================================================
# `API-20` 软删除
# =============================================================================
def soft_delete(db: Session, task_id: int, *, user_id: int = GUEST_USER_ID) -> None:
    """软删除任务（`API-20-1`）。

    🔒 **只置 `tasks.is_deleted = 1`**，`predictions` / `metrics`
       一律不动 —— 验收要求「删除后可通过数据库查回，满足可追溯」。
       级联软删关联表反而会让「查回」变复杂（`API-20-2`）。

    🔒 幂等：重复删除同一任务仍返回成功。用户点了两次删除弹窗
       报错「任务不存在」会让人以为第一次没删掉。

    ⚠️ **归属校验不能走 `_owned_task`**：它现在把软删除视为不存在
       （`EP-7` 修复），而重复删除需要在这之前就返回成功。所以这里
       按「含已删除」的口径查一次，**先判归属、再判幂等**：
       三步顺序 = 不存在 → `40401` / 非本人 → `40301` / 已删除 → 直接成功。

    📌 三个错误码的分工（`EP-9` 收敛后）：
       `40401` 真的没有这个任务；`40301` 有但不是你的（`NFR-SEC-05` PRD P0）；
       本人重复删除返回成功而不是报错。三者不可合并 ——
       合并任意两个，前端都无法给出正确的引导（"去列表重选" vs "无权访问"）。
    """
    task = task_dao.get_by_id(db, task_id)
    if task is None:
        # 不存在 → 40401（`API-20` 既有契约，EP-7 已固化）
        raise BizError(
            ErrorCode.TASK_NOT_FOUND,
            f"任务 {task_id} 不存在，请确认任务 ID 是否正确",
            {"task_id": task_id},
        )
    if int(task.user_id or 0) != int(user_id):
        # 🔒 `NFR-SEC-05`：**越权返回 `40301` 而非 `40401`**。
        #    `EP-9` 与 `result_service._owned_task` 一并收敛 —— 否则会出现
        #    「同一个别人的任务，删除接口说 404、对比接口说 403」的口径分裂。
        #    ⚠️ 归属判断必须在「已删除 → 幂等返回」**之前**：
        #       反过来会变成「别人删过的任务我删也算成功」，等于用返回值
        #       确认了该 id 存在（`B-20` 要防的正是这件事）。
        raise BizError(
            ErrorCode.FORBIDDEN,
            f"无权删除任务 {task_id}，请确认任务 ID 是否正确",
            {"task_id": task_id},
        )
    if int(task.is_deleted or 0) == 1:
        return  # 幂等：重复删除同一任务仍返回成功（`API-20-2`）
    task_dao.soft_delete(db, task.id)
    db.commit()


# =============================================================================
# `UX-P04` 工作台概览
# =============================================================================
def workbench_summary(
    db: Session, *, user_id: int = GUEST_USER_ID, recent_limit: int = 5
) -> dict[str, Any]:
    """工作台概览（`UX-P04`）。

    📌 **最近任务直接复用 `API-17` 的列表函数**，不另写一条查询 ——
       两处口径一旦分叉，「工作台最近任务」和「历史列表首屏」
       就会对不上（`DOD-EP7` 的验收点之一）。
    """
    history = list_history(db, user_id=user_id, page=1, size=recent_limit)
    items = history["items"]
    total = history["total"]

    succeeded = sum(1 for i in items if i["status"] == "succeeded")
    failed = sum(1 for i in items if i["status"] == "failed")
    mapes = [i["weighted_mape"] for i in items if i["weighted_mape"] is not None]
    over = sum(1 for i in items if i["is_over_threshold"])

    return {
        "is_guest": int(user_id) == int(GUEST_USER_ID),
        "stats": {
            "total": total,
            "recent_succeeded": succeeded,
            "recent_failed": failed,
            "recent_over_threshold": over,
            "recent_mape_avg": round(sum(mapes) / len(mapes), 2) if mapes else None,
        },
        "recent": items,
        "has_dataset": bool(total) or _has_any_dataset(db, user_id),
        "has_model": _model_ready(),
    }


def _has_any_dataset(db: Session, user_id: int) -> bool:
    from app.dao import dataset_dao

    return bool(dataset_dao.list_by_user(db, user_id, limit=1))


def _model_ready() -> bool:
    from app.core import model_holder

    try:
        model_holder.assert_ready()
        return True
    except Exception:  # noqa: BLE001 - 概览不该因模型未就绪而失败
        # `R-12`：原来是裸 return False。工作台因此少一张「可发起预测」的提示，
        # 若真相是模型**加载失败**（而不是还没加载），这里就是唯一线索。
        logger.debug("模型未就绪（工作台降级）", exc_info=True)
        return False