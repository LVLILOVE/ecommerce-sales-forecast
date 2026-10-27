"""
=============================================================================
预测结果服务（`API-14` / `API-15` / `API-16`）
-----------------------------------------------------------------------------
版本     : V1.0（EP-6）
依据文档 : 《开发技术文档 V1.0》§5.6（`API-14` ~ `API-16`）、§4.7.2 三向映射表
           《项目开发实施方案 V1.0》§7.7（`WS-6-01` ~ `WS-6-09`、`DOD-EP6`）
           《UI/UX 设计文档 V1.0》§8.4（`UX-P08-01` ~ `UX-P08-12`）
-----------------------------------------------------------------------------
🔒 **本模块不做任何指标重算**（`WS-6-01` ~ `WS-6-04` 的落地方式）

   这是整个 `EP-6` 最要紧的一条纪律。结果页展示的每个数字都必须来自
   `EP-5` 落库的 `metrics` / `predictions` 表，也就是算法在**回测**时算出的
   同一批值。若这里「顺手用 predictions 表再算一遍 MAPE」，会出现两个后果：

     ① **数字必然不同** —— `predictions` 表存的是**在线预测**（未来日期、
        `actual_value` 恒 NULL，见 `predict_service._build_prediction_rows`
        的第 3 条口径），根本不存在真值，拿它算误差只会得到空集或错误值；
     ② **「两套数字」复现** —— 这正是本项目原型阶段踩过的坑（`MEMORY.md`
        「已产出交付物」一节），当时原型手写指标与算法产出对不上，
        靠 `sync_prototype_data.py` 事后同步才收场。

   所以本模块只做三件事：**读表 → 装配响应 → 处理导出**。

🔒 **`distribution` / `timing_ms` 来自 `metrics` 的 JSON 列，不是重算**
   `EP-5` 落库时已把直方图分箱（`distribution_json`）与分阶段耗时
   （`timing_json`）序列化进 TEXT 列。这里反序列化后**原样透出**。
   若临时重算，`timing_ms` 会退化成「只有结果页这次读的耗时」，
   而那不是任务真实耗时 —— 与用户在执行页看到的时间轴对不上。

⚠️ **`coverage` 的三项计数必须恒等于 `total`（`API-14-1` / `UX-AC-06`）**
   本模块在 `_coverage()` 里显式做了一次**闭合校验**：三项之和 ≠ total 时
   记 WARNING 日志并**以 `tasks` 表冗余列为准**回填，而不是把不一致原样
   透给前端。理由：`predictions` 表的 L3 只落**一行**（EP-5 决策），
   用 `count_by_level` 数行数得到的是「明细行数」而非「序列数」，
   直接当覆盖率用会把 L3 显示成 30 倍 —— 这类错误肉眼极难发现，
   必须在服务层就拦住。
=============================================================================
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy.orm import Session

from app.core import csv_safety
from app.core.config import settings
from app.core.db import EXPORT_TMP_DIR, SessionLocal
from app.core.deps import GUEST_USER_ID
from app.core.errors import BizError, ErrorCode
from app.dao import dataset_dao, metric_dao, prediction_dao, task_dao
from app.models.metric import THRESHOLD_MAPE
from app.services import dataset_service
from app.validators import load_csv

logger = logging.getLogger(__name__)

__all__ = [
    "get_result",
    "list_detail",
    "prepare_export",
    "iter_export_chunks",
    "ExportSpec",
    "EXPORT_HEADER",
    "EXPORT_FILENAME_PREFIX",
]

# 🔒 `API-16` 固定导出列序（`FR-VIS-05` / `UX-P08-08`）。
#
# ⚠️ **列名与库表列名刻意不同**：`date`（不是 `forecast_date`）、
#    `forecast_sales`（不是 `predicted_value`）、`actual_sales`、
#    `error`。原因有两个，都不是洁癖：
#     ① 这些是**交付给业务方**的表头，用库列名会把实现细节泄漏出去；
#     ② `exported.csv` 要能直接被业务方用 Excel 二次处理，
#        列名混进 `forecast_date` / `granularity_level` 这种内部术语很难解释。
#    ⚠️ `granularity_level` 保留原名 —— 它是 `UX-P08-07` 要求明细里
#       必须可见的分级标记，且业务方确实需要按它筛选（CSV 无筛选功能）。
EXPORT_HEADER: tuple[str, ...] = (
    "sku_id",
    "warehouse_id",
    "date",
    "forecast_sales",
    "granularity_level",
    "actual_sales",
    "error",
)
EXPORT_FILENAME_PREFIX = "预测结果"

# `API-16-2`：小于该大小直接流式返回、不落盘（`NFR-REL-05`）
EXPORT_DUMP_THRESHOLD_BYTES = 5 * 1024 * 1024


# =============================================================================
# 归属与存在性
# =============================================================================
def _owned_task(db: Session, task_id: int, user_id: int) -> Any:
    """取任务并校验归属（`BE-41` 两分支顺序，不可简化）。

    ⚠️ 游客（`user_id == GUEST_USER_ID`）只能看**自己跑出来的游客任务**：
       判定条件是「任务的 `user_id` 同样是游客哨兵值」。这条分支不是
       「游客可见全部」，而是「游客只看游客的」—— 否则任何游客都能
       翻到别人的任务 id 去看预测结果。

    🔒 **软删除的任务视为不存在**（`EP-7` 修复的真实缺陷）。
       原实现用 `task_dao.get_by_id`，它不带 `is_deleted` 过滤 ——
       于是用户点了「删除」之后，任务确实从 `API-17` 列表消失了，
       但**直接访问 `/history/{id}` 仍能打开完整结果**。
       这不是「可追溯」而是「删除没生效」：`FR-HIS-05` 的可追溯是
       指**数据仍在库中可被管理员查回**，不是指用户本人还能从界面访问。
       列表与详情对同一个任务的判断必须一致，否则用户会以为删除失败
       又去点一次，或者干脆认为系统有 bug。
    """
    task = task_dao.get_by_id(db, task_id)
    if task is None or int(task.is_deleted or 0) == 1:
        raise BizError(
            ErrorCode.TASK_NOT_FOUND,
            f"任务 {task_id} 不存在或已被删除，请返回历史任务列表重新选择",
            {"task_id": task_id},
        )
    if int(task.user_id or 0) != int(user_id):
        # 🔒 `NFR-SEC-05`（PRD P0）：**越权返回 403 而非 404**。
        #    `EP-9` 全量回归时发现原实现返回 `40401`（与「任务不存在」同码），
        #    与技术文档 §9.4 `B-20` / §4.4.4 / §8 错误码表三处判据都相悖，
        #    且与 `datasets`（`dataset_service` 返 `40301`）、
        #    `history/compare`（`history_service` 返 `40301`）**口径不一致** ——
        #    同一个「别人的资源」，有的接口说 403、有的说 404，前端无法统一处理。
        #    PRD 措辞是「越权返回 403 而非 404（避免资源探测）」，故按 PRD 收敛为 40301。
        #    ⚠️ 「任务不存在」与「已软删除」仍返回 `40401`（见上）：那才是真的没有，
        #       两者必须是不同的码，否则 40301 就失去了信息量。
        raise BizError(
            ErrorCode.FORBIDDEN,
            "无权查看该任务的结果，请确认任务 ID 是否正确",
            {"task_id": task_id},
        )
    return task


def _require_succeeded(task: Any) -> None:
    """只有成功任务才有结果。

    🔒 失败任务返回 `40401` 而不是「空结果」：半成品不落盘
       （`UX-P08-12`），返回一个结构完整但全为空的结果页，
       用户会以为「预测成功但数值都是 0」—— 那比报错糟糕得多。
    """
    if task.status != "succeeded":
        raise BizError(
            ErrorCode.TASK_NOT_FOUND,
            f"任务 {task.id} 当前状态为「{task.status}」，尚未产出结果。"
            "失败任务不落盘半成品，请在历史任务列表中查看失败原因或重新运行",
            {"task_id": task.id, "status": task.status, "error_code": task.error_code},
        )


def _require_metrics(db: Session, task_id: int) -> Any:
    metric = metric_dao.get_by_task(db, task_id)
    if metric is None:
        raise BizError(
            ErrorCode.TASK_NOT_FOUND,
            f"任务 {task_id} 的指标数据缺失，可能在写库后被中断。"
            "请重新运行该任务",
            {"task_id": task_id},
        )
    return metric


def _dataset(db: Session, dataset_id: int) -> Any:
    """取数据集实体。

    🔒 用 `dataset_dao.get_by_id`（内部读）而**不是** `get_owned`：
       任务的归属已由 `_owned_task` 校验过，此处再按 `dataset.user_id`
       过滤一次，对**游客任务**会直接查不到（游客哨兵 `user_id=0` 的
       数据集 `user_id` 也是 0，恰好能过；但登录用户 A 复看自己的任务时，
       若数据集归属被改过就会误判 404）。归属校验的唯一真源是任务。
    """
    dataset = dataset_dao.get_by_id(db, dataset_id)
    if dataset is None:  # pragma: no cover - 外键 RESTRICT，正常不会发生
        raise BizError(
            ErrorCode.DATASET_NOT_FOUND,
            f"任务关联的数据集（id={dataset_id}）不存在，请重新上传后运行",
            {"dataset_id": dataset_id},
        )
    return dataset


# =============================================================================
# `API-14` 结果汇总
# =============================================================================
def get_result(
    db: Session,
    task_id: int,
    *,
    user_id: int = GUEST_USER_ID,
    with_series: bool = True,
    series_limit: int = 50,
    level: str | None = None,
) -> dict[str, Any]:
    """结果页全部数据（`UX-P08`）。

    🔒 耗时预算 ≤ 1 s（`NFR-PERF-02`）。本函数**不做任何算法计算**，
       最重的一步是 `prediction_dao` 按序列分组取曲线（`series_limit` 条
       × horizon 天，实测 50 × 30 = 1,500 行，SQLite 上是毫秒级）。
    """
    task = _owned_task(db, task_id, user_id)
    _require_succeeded(task)
    metric = _require_metrics(db, task_id)
    dataset = _dataset(db, task.dataset_id)

    payload: dict[str, Any] = {
        "task": _task_brief(task),
        "dataset": _dataset_brief(dataset),
        "metrics": _metric_bundle(metric),
        "coverage": _coverage(db, task),
        "distribution": _distribution(metric),
        "timing_ms": _timing_ms(metric, task),
        "is_over_threshold": bool(metric.is_over_threshold),
        "eval_note": metric.eval_note or "",
        "detail_total": prediction_dao.count_by_task(db, task_id),
        "series": [],
    }
    if not with_series:
        return payload

    series, truncated = _series_curves(db, task, limit=series_limit, level=level)
    payload["series"] = series
    # 🔒 `API-14-3`：`truncated` 让前端能诚实告知「只画了前 N 条」，
    #    而不是让用户以为「只有这 50 条序列」。
    payload["truncated"] = truncated
    return payload


def _task_brief(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_name": task.task_name,
        "status": task.status,
        "horizon": task.horizon,
        "granularity": task.granularity,
        "model_version": task.model_version,
        "device": task.device or "cpu",
        "device_fallback": int(task.device_fallback or 0),
        "series_total": int(task.series_total or 0),
        "duration_ms": int(task.duration_ms or 0),
        "created_at": task.created_at,
        "finished_at": task.finished_at,
    }


def _dataset_brief(dataset: Any) -> dict[str, Any]:
    return {
        "id": dataset.id,
        "file_name": dataset.file_name,
        "row_count": int(dataset.row_count or 0),
        "sku_count": int(dataset.sku_count or 0),
        "warehouse_count": int(dataset.warehouse_count or 0),
        "date_start": dataset.date_start or "",
        "date_end": dataset.date_end or "",
    }


def _metric_bundle(metric: Any) -> dict[str, Any]:
    """指标包（加权口径 + 基线 + 达标占比 + 分组）。

    🔒 **不做四舍五入**：库里是多少就返回多少。技术文档的响应示例写的是
       `14.26`（两位小数），但那是文档为了可读性手写的；
       实际落库值是 `14.2585`。前端格式化时再决定保留几位 ——
       接口层一旦 `round()`，`DOD-EP6` 的「与 `eval_report.json` 逐项一致」
       就需要额外的容差断言，且用户点开详情看到的数字会与算法产出对不上。
    """
    return {
        "weighted_mape": _num(metric.mape),
        "weighted_mae": _num(metric.mae),
        "weighted_rmse": _num(metric.rmse),
        "baseline_mape": _num(metric.baseline_mape),
        "baseline_mae": _num(metric.baseline_mae),
        "baseline_rmse": _num(metric.baseline_rmse),
        "mae_improve_pct": _num(metric.mae_improve_pct),
        "rmse_improve_pct": _num(metric.rmse_improve_pct),
        "series_evaluated": int(metric.series_evaluated or 0),
        "series_passed": int(metric.series_passed or 0),
        "series_pass_rate": _num(metric.series_pass_rate),
        "l1_mape": _num(metric.l1_mape),
        "l2_mape": _num(metric.l2_mape),
    }


def _coverage(db: Session, task: Any) -> dict[str, Any]:
    """覆盖率（`FR-VIS-07` / `UX-P08-04`）。

    🔒 **计数口径 = 序列数，且以 `tasks` 表冗余列为真源**。
       不用 `prediction_dao.count_by_level()` —— 那是**明细行数**
       （L1 序列 × horizon 天），与序列数差一个 horizon 倍。
       `EP-5` 落库时已把序列级计数冗余到 `tasks.l1_count/l2_count/l3_count`，
       那里才是覆盖率要的量（`eval_report.json` 的 coverage 段同源）。
    """
    l1 = int(task.l1_count or 0)
    l2 = int(task.l2_count or 0)
    l3 = int(task.l3_count or 0)
    total = int(task.series_total or 0)

    # 闭合校验（`API-14-1`）：三项之和必须等于 total。
    # 不等意味着分级或统计环节有缺陷 —— 此时以三项之和为准并告警，
    # 因为「三项之和」至少内部自洽，而 total 可能来自另一条统计路径。
    if total and (l1 + l2 + l3) != total:
        logger.warning(
            "coverage 计数不闭合：L1=%s L2=%s L3=%s sum=%s total=%s（task=%s）",
            l1, l2, l3, l1 + l2 + l3, total, task.id,
        )
        total = l1 + l2 + l3
    elif not total:
        total = l1 + l2 + l3

    predictable = l1 + l2          # L3 无历史，不可预测
    pct = lambda n: round(n / total * 100, 2) if total else 0.0  # noqa: E731
    return {
        "L1": l1, "L2": l2, "L3": l3, "total": total,
        "L1_pct": pct(l1), "L2_pct": pct(l2), "L3_pct": pct(l3),
        "predictable": predictable,
        "predictable_pct": pct(predictable),
    }


def _distribution(metric: Any) -> dict[str, Any]:
    """逐序列 MAPE 分布（`FR-VIS-08` 直方图）。

    `EP-5` 已把 20 个分箱压成 `distribution_json`；这里只反序列化。
    🔒 反序列化失败（列内容损坏）时返回**空分布**而不是抛错：
       直方图只是辅助展示，不该让整个结果页打不开。
    """
    fallback = {
        "median": None, "p75": None, "p90": None, "max": None,
        "over_count": 0, "threshold": THRESHOLD_MAPE, "buckets": [],
    }
    raw = _loads(metric.distribution_json)
    if not isinstance(raw, dict):
        return fallback
    return {
        "median": _num(raw.get("median")),
        "p75": _num(raw.get("p75")),
        "p90": _num(raw.get("p90")),
        "max": _num(raw.get("max")),
        "over_count": int(raw.get("over_count") or 0),
        "threshold": float(raw.get("threshold") or THRESHOLD_MAPE),
        "buckets": [int(b) for b in (raw.get("buckets") or [])],
    }


def _timing_ms(metric: Any, task: Any) -> dict[str, Any]:
    """分阶段耗时（执行页时间轴的回放口径）。

    ⚠️ `timing_json` 里有**两套**（`stages` 单遍 / `stages_two_pass_ms` 两遍合计）。
       `API-14` 契约里的 `timing_ms` 是 5 键结构（与 `eval_report.json` 可比），
       故这里取**单遍** `stages`；`total_ms` 用 `tasks.duration_ms`
       （端到端真实墙钟，含两遍与落库），与用户在执行页看到的总耗时一致。
    """
    raw = _loads(metric.timing_json)
    stages = raw.get("stages") if isinstance(raw, dict) else None
    stages = stages if isinstance(stages, dict) else {}
    return {
        "preprocess_ms": int(stages.get("preprocess") or 0),
        "features_ms": int(stages.get("features") or 0),
        "inference_ms": int(stages.get("inference") or 0),
        "metrics_ms": int(stages.get("metrics") or 0),
        "total_ms": int(task.duration_ms or 0),
    }


def _series_curves(
    db: Session,
    task: Any,
    *,
    limit: int,
    level: str | None,
    predicted_only: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """曲线数据（`FR-VIS-01` / `UX-P08-05`）。

    🔒 **本函数是本模块唯一「读明细表拼曲线」的地方，且刻意不含任何误差计算**：
       `mape` / `passed` 取自 `EP-5` 回测时算好的**序列级**结果
       （`metrics.distribution_json` 只存了聚合分位数，逐序列明细未落库），
       故这里**不返回**逐序列 MAPE —— 返回 `null` 而不是现算。
       现算的诱惑很大（明细表有 `actual_value`），但在线预测该列恒 NULL，
       算了也是空；回测的真值在第 1 遍的 `metric_records` 里，未落库。
       ⇒ 结果页的序列级达标情况由**汇总指标**（`series_pass_rate`）表达，
          曲线图只画真值 / 预测 / 基线三条线，不逐序列标达标 —— 与
          `UX-P08-05` 的要求一致（它要的是三条线 + 预测起点，没要逐序列达标）。

    📌 **历史段从数据集 CSV 现读**（`history_dates` / `history_sales`）：
       `predictions` 表只存未来预测段，历史段没有落库。曲线图左侧实线段
       必须有真实历史，否则「预测起点」无从标注（`UX-P08-05`）。
       这里只读一次 CSV 并按序列切片，14,305 行实测 < 200 ms。
    """
    grouped = _series_prediction_rows(db, task.id)
    if level:
        # 🔒 `level` 过滤在**分组后**做：先取全量再按序列级分级筛，
        #    若改成 SQL 层 `WHERE granularity_level = ?` 再取 limit 条，
        #    「前 N 条 L1 曲线」会随 limit 变化而漂移到不同序列，无法复现。
        grouped = {
            k: v for k, v in grouped.items() if v["level"] == level
        }
    if not grouped:
        return [], False

    # ⚠️ `grouped` 是 dict，**不能切片**。按插入序（即 `list_by_task` 的
    #    sku_id → warehouse_id → forecast_date 序）取前 N 个键：
    #    保证同一序列的多行永远相邻，且分页/截断结果稳定可复现。
    items = list(grouped.items())
    truncated = len(items) > limit
    if truncated:
        items = items[:limit]
    rows = dict(items)

    # ⚠️ `rows` 的键**就是** `(sku, warehouse)` 元组，直接传 `set(rows)`。
    #    写成 `{s for s, _ in rows}` 会去解包 dict 的**字符串键**，
    #    抛 `too many values to unpack`（仓库名 "上海仓" 有 3 个字符）。
    #
    # ⚠️ `predicted_only=True`（`API-18` 对比页用）时**跳过历史段读取**：
    #    历史段要从数据集 CSV 现读并按序列切片，是本函数最贵的一步；
    #    而对比页多条曲线叠在一起时历史段会完全重合（谁也看不清），
    #    且对比要回答的是「不同步长预测差多少」，历史段不参与这个问题。
    #    顺带把 4 个任务的 CSV 读取开销降为 0 —— 对比接口 ≤ 3 s 的关键。
    history = {} if predicted_only else _series_history(
        db, task, set(rows), len(next(iter(rows.values()))["dates"])
    )
    curves: list[dict[str, Any]] = []
    for (sku, warehouse), seg in rows.items():
        level_of = seg["level"]
        dates = seg["dates"]
        predicted = seg["predicted"]
        baseline = seg["baseline"]
        hist_dates, hist_sales = (
            ([], []) if predicted_only else history.get((sku, warehouse), ([], []))
        )
        curves.append({
            "key": f"{sku}-{warehouse}",
            "sku_id": sku,
            "warehouse_id": warehouse,
            "granularity_level": level_of,
            "mape": None,
            "passed": False,
            "forecast_dates": dates,
            "predicted": predicted,
            # 🔒 在线预测的真值尚未发生，`actual` 恒为 null（`API-15-1` 同源口径）
            "actual": None,
            "baseline": baseline,
            "history_dates": hist_dates,
            "history_sales": hist_sales,
        })
    return curves, truncated


def _series_prediction_rows(
    db: Session, task_id: int
) -> dict[tuple[str, str], dict[str, Any]]:
    """把明细行按 `(sku, warehouse)` 聚成序列。

    ⚠️ **L3 只落一行**（`EP-5` 决策：`predicted_value` 存历史均值作占位）。
       故 L3 的曲线段长度为 1 而非 horizon —— 前端据 `granularity_level == 'L3'`
       判定为「不可预测」，**不画数值预测**（`UX-P08-06` / `UX-AC-10`），
       只显示降级说明。这里不做补齐，补齐等于用均值伪装成预测（`UX-P08-06` 明令禁止）。
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for p in prediction_dao.list_by_task(db, task_id):
        key = (p.sku_id, p.warehouse_id)
        seg = grouped.get(key)
        if seg is None:
            seg = grouped[key] = {
                "level": p.granularity_level,
                "dates": [], "predicted": [], "baseline": [],
            }
        seg["dates"].append(p.forecast_date)
        seg["predicted"].append(_round(float(p.predicted_value)))
        seg["baseline"].append(
            _round(float(p.baseline_value)) if p.baseline_value is not None else None
        )
    return grouped


def _series_history(
    db: Session, task: Any, wanted: set[tuple[str, str]], window: int
) -> dict[tuple[str, str], tuple[list[str], list[float]]]:
    """取曲线左侧历史段（预测起点之前的真实销量）。

    🔒 只对**要画的那几条序列**取历史（`wanted` 来自 `series_limit` 截断后），
       否则 1,000 序列全取一遍会把 1 秒预算吃掉大半。

    ⚠️ CSV 缺失 / 读取失败时返回空 dict，曲线退化为「只有预测段」——
       这比让整个结果页 500 好：图还能看，只是没有左侧实线段。
    """
    if not wanted or window <= 0:
        return {}
    df = _read_frame(db, task.dataset_id)
    if df is None:
        return {}

    from app.algo import config as algo_config

    out: dict[tuple[str, str], tuple[list[str], list[float]]] = {}
    try:
        sub_all = df[
            df[algo_config.COL_SKU].isin({s for s, _ in wanted})
            & df[algo_config.COL_WAREHOUSE].isin({w for _, w in wanted})
        ]
        for (sku, warehouse), grp in sub_all.groupby(
            [algo_config.COL_SKU, algo_config.COL_WAREHOUSE], sort=False
        ):
            grp = grp.sort_values(algo_config.COL_DATE).tail(window)
            dates = [str(d) for d in grp[algo_config.COL_DATE].tolist()]
            sales = [_round(float(v)) for v in grp[algo_config.COL_SALES].tolist()]
            out[(str(sku), str(warehouse))] = (dates, sales)
    except Exception:  # noqa: BLE001 - 切片失败只降级，不影响主流程
        logger.warning("历史段切片失败，曲线将只有预测段（task=%s）", task.id, exc_info=True)
    return out


def _read_frame(db: Session, dataset_id: int) -> Any:
    """读数据集 CSV 为 DataFrame；失败返回 `None`（调用方降级）。

    ⚠️ 走 `validators.load_csv`（`detect_encoding` + GB18030 兜底）
       与上传/预测阶段**同源**。这里刻意复用而不是自己 `pd.read_csv`：
       上传时按 GB18030 存的历史文件若在这里按 UTF-8 读，会直接抛
       `UnicodeDecodeError`，曲线左侧实线段整段消失。
    """
    dataset = dataset_dao.get_by_id(db, dataset_id)
    if dataset is None:  # pragma: no cover
        return None
    path = dataset_service._abs(dataset)  # noqa: SLF001 - 同包路径还原工具
    if not path.exists():
        logger.warning("数据集文件已不存在，历史段降级为空（id=%s）", dataset_id)
        return None
    try:
        df, _enc = load_csv(path, None)
        return df
    except Exception:  # noqa: BLE001
        logger.warning("数据集读取失败，历史段降级为空（id=%s）", dataset_id, exc_info=True)
        return None


# =============================================================================
# `API-15` 结果明细分页
# =============================================================================
def list_detail(
    db: Session,
    task_id: int,
    *,
    user_id: int = GUEST_USER_ID,
    page: int = 1,
    size: int = 20,
    sort_by: str = "sku_id",
    order: str = "asc",
    level: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """结果明细分页（`FR-VIS-03`：按任意列排序 + 分页，默认 20 行/页）。

    🔒 `total` 与 `items` 必须**同条件**统计，否则最后一页空白（见
       `prediction_dao.list_sorted_paged` 的注释）。
    """
    task = _owned_task(db, task_id, user_id)
    _require_succeeded(task)

    items = prediction_dao.list_sorted_paged(
        db, task_id,
        page=page, size=size, sort_by=sort_by, order=order,
        level=level, keyword=keyword,
    )
    total = prediction_dao.count_filtered(db, task_id, level=level, keyword=keyword)
    return {
        "items": [_detail_row(p) for p in items],
        "page": page,
        "size": size,
        "total": total,
        "pages": (total + size - 1) // size if size else 0,
    }


def _detail_row(p: Any) -> dict[str, Any]:
    """明细行。

    🔒 `error` / `error_pct` 在**无真值时为 null**，不是 0
       （`API-16` 的 `actual_sales` 同理）。填 0 会被业务方读成
       「预测完全准确」，这是彻头彻尾的误导。
    """
    predicted = float(p.predicted_value)
    actual = None if p.actual_value is None else float(p.actual_value)
    error = None if actual is None else abs(actual - predicted)
    error_pct = (
        None if (actual is None or actual == 0) else round(error / abs(actual) * 100, 2)
    )
    return {
        "sku_id": p.sku_id,
        "warehouse_id": p.warehouse_id,
        "forecast_date": p.forecast_date,
        "predicted_value": _round(predicted),
        "actual_value": None if actual is None else _round(actual),
        "baseline_value": None if p.baseline_value is None else _round(float(p.baseline_value)),
        "granularity_level": p.granularity_level,
        "error": None if error is None else _round(error),
        "error_pct": error_pct,
    }


# =============================================================================
# `API-16` 导出
# =============================================================================
# =============================================================================
# 导出（`API-16` / `FR-VIS-05`）
# -----------------------------------------------------------------------------
# 🔒 **流式**：本模块唯一一处「内存占用不随数据量增长」的实现。
#
#    2026-10-26 修复的历史缺陷：`iter_all_for_export` 早已在 DAO 层用
#    `yield_per=2000` 分批取数，但本层用 `io.StringIO` 把全部行收口后
#    再 `.encode()`，接口层又 `Response(content=...)` 整体驻留 ⇒
#    上限 300 万行时峰值约 360 MB/次，**DAO 的流式被完全抵消**，
#    与 `prediction_dao.iter_all_for_export` 自身 docstring 的说法矛盾。
#    现在改为「生成器逐批产出 bytes + `StreamingResponse`」。
#
# 🔒 **生成器必须自己开会话**：FastAPI 0.106+ 在发送 `StreamingResponse`
#    之前就会退出 `yield` 依赖，复用请求会话会拿到已关闭的连接。
#    所以 `iter_export_chunks()` 只接收 `task_id`，不接收 `Session`。
#
# 🔒 **留档路径必须走 `settings.export_dir`**：原实现硬编码
#    `base_dir / "data" / "exports"`，导致 `verify_*` 脚本设置的
#    `EXPORT_DIR` 隔离对本项失效（只因演示数据行数达不到 5 MB 落盘阈值
#    才一直没暴露）。
# =============================================================================

EXPORT_CHUNK_ROWS = 2000            # 每批写出的行数（与 DAO 的 yield_per 对齐）
_XLSX_STREAM_CHUNK = 64 * 1024      # xlsx 回放的分块大小（64 KB）

# CSV 注入防护：以这些字符开头的**文本**单元格会被 Excel 当公式执行（DDE）。
#   只作用于用户可控的文本列（`sku_id` / `warehouse_id`）；
#   数值列绝不能加前缀，否则 `-12` 会变成 `'-12` 破坏类型。
_CSV_UNSAFE_PREFIX = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ExportSpec:
    """导出**描述**（不含内容）。内容由 `iter_export_chunks()` 按需产出。"""

    fmt: str
    name: str
    mime: str
    task_id: int | None = None          # csv：流式读源
    xlsx_path: Path | None = None       # xlsx：已落盘，待回放
    dump: bool = False                  # 是否保留留档（`API-16-2`）

    @property
    def suffix(self) -> str:
        return self.name.rsplit(".", 1)[-1]


def prepare_export(
    db: Session,
    task_id: int,
    *,
    user_id: int,
    fmt: str = "csv",
) -> ExportSpec:
    """校验归属与任务状态，返回导出描述（**不生成内容**）。

    🔒 **导出要求登录**（`FR-USR-08` / `JT-10`）：游客能看结果页，
       但不能把数据带走。调用方必须用 `CurrentUser` 而非 `OptionalUserId`。
    🔒 **列序固定**（`UX-P08-08`），见 `EXPORT_HEADER` 注释。
    """
    task = _owned_task(db, task_id, user_id)
    _require_succeeded(task)

    # 预估体积：命中阈值才落盘留档，否则只走流式（`API-16-2` / `NFR-REL-05`）
    total_rows = prediction_dao.count_by_task(db, task_id)
    dump = total_rows * 160 >= EXPORT_DUMP_THRESHOLD_BYTES
    name = f"{EXPORT_FILENAME_PREFIX}_{task_id}_{datetime.now():%Y%m%d%H%M%S}.{fmt}"

    if fmt == "xlsx":
        path = _write_xlsx_to_disk(db, task_id, name if dump else None)
        return ExportSpec(
            fmt="xlsx",
            name=name,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            xlsx_path=path,
            dump=dump,
        )

    return ExportSpec(
        fmt="csv",
        name=name,
        mime="text/csv; charset=utf-8",
        task_id=task_id,
        dump=dump,
    )


def iter_export_chunks(spec: ExportSpec) -> Iterator[bytes]:
    """按块产出导出内容（供 `StreamingResponse` 消费）。"""
    if spec.fmt == "xlsx":
        assert spec.xlsx_path is not None
        yield from _file_chunks(spec.xlsx_path, delete=not spec.dump)
        return
    assert spec.task_id is not None
    yield from _iter_csv_chunks(spec.task_id, dump=spec.dump, name=spec.name)


def _iter_csv_chunks(task_id: int, *, dump: bool, name: str) -> Iterator[bytes]:
    """逐批产出 CSV 字节。

    内存占用 ≈ `EXPORT_CHUNK_ROWS` 行，**与总行数无关** —— 这是本函数存在的理由。
    """
    dump_fh = _open_dump(name) if dump else None
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def _flush() -> bytes:
        chunk = buf.getvalue().encode("utf-8")
        buf.seek(0)
        buf.truncate(0)
        if dump_fh is not None:
            dump_fh.write(chunk)
        return chunk

    try:
        # 🔒 BOM 必须最先发出：不带 BOM 时 Excel 双击打开中文列必乱码（`API-16-1`）
        bom = "\ufeff".encode("utf-8")
        if dump_fh is not None:
            dump_fh.write(bom)
        yield bom

        writer.writerow(EXPORT_HEADER)
        # 见本节说明：不能复用请求的 Session
        with SessionLocal() as own_db:
            for i, row in enumerate(
                prediction_dao.iter_all_for_export(own_db, task_id), 1
            ):
                writer.writerow(_export_cells(row))
                if i % EXPORT_CHUNK_ROWS == 0:
                    yield _flush()
        if buf.tell():
            yield _flush()
    finally:
        if dump_fh is not None:
            try:
                dump_fh.close()
            except OSError:  # pragma: no cover - 关闭失败无需影响下载
                logger.warning("导出留档文件关闭失败", exc_info=True)


def _export_cells(p: Any) -> list[str]:
    """导出一行的单元格。

    🔒 `actual_sales` / `error` 无真值时写**空字符串**，不写 0（`API-16`）。

    🔒 `error` 用库里的 `predicted_value` 与 `actual_value` 现算 —— 这是
       **恒等式**（`|y - ŷ|`），不是模型指标，故不违反「不重算指标」纪律；
       若真值为空则留空。

    🔒 **文本列做 CSV 注入转义**：`sku_id` / `warehouse_id` 来自用户上传的
       CSV，若以 `= + - @ Tab CR` 开头，Excel 打开导出文件时会当公式执行
       （DDE / CSV Injection）。加 `'` 前缀即可降级为纯文本。
    """
    actual = p.actual_value
    err = "" if actual is None else f"{abs(float(actual) - float(p.predicted_value)):.2f}"
    return [
        _csv_text(p.sku_id),
        _csv_text(p.warehouse_id),
        p.forecast_date,
        f"{float(p.predicted_value):.2f}",
        p.granularity_level,
        "" if actual is None else f"{float(actual):.2f}",
        err,
    ]


def _csv_text(value: Any) -> str:
    """文本单元格的 CSV 注入防护（见 `_CSV_UNSAFE_PREFIX`）。

    🔴 实现已上收到 `app/core/csv_safety.py`：错误明细导出（`API-11`）
    之前漏了这层防护，而它下载的同样是**用户上传的原始行** ——
    威胁模型与结果导出完全相同。两处共用一份实现，才不会再次分叉。
    """
    return csv_safety.safe_text(value)


def _write_xlsx_to_disk(db: Session, task_id: int, keep_name: str | None) -> Path:
    """生成 xlsx 并落盘，返回路径。

    `write_only=True` + `save(路径)` 让 openpyxl 直接流进 zip，
    不在内存里保留整张表 —— 这是 xlsx 分支也能撑住大数据量的原因。

    ⚠️ 优先用 `openpyxl`；未安装则**明确抛错**由上层转成可读提示。
       绝不用「CSV 内容 + xlsx 扩展名」糊弄（Excel 会拒绝打开）。
    """
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise BizError(
            ErrorCode.IO_ERROR,
            "当前服务未安装 Excel 导出组件（openpyxl），"
            "请改用 CSV 格式导出，或联系管理员安装依赖",
            {"format": "xlsx"},
        ) from exc

    if keep_name is None:
        target_dir = EXPORT_TMP_DIR
        path = target_dir / f".xlsx-{uuid.uuid4().hex}.xlsx"
    else:
        target_dir = settings.export_dir
        path = target_dir / keep_name
    target_dir.mkdir(parents=True, exist_ok=True)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("预测结果")
    ws.append(list(EXPORT_HEADER))
    for row in prediction_dao.iter_all_for_export(db, task_id):
        ws.append(_export_cells(row))
    wb.save(path)
    return path


def _open_dump(name: str) -> Any | None:
    """打开留档文件。

    ⚠️ 落盘失败只告警不抛：导出内容已经生成好了，为一个「留档」功能
       让整次导出失败是本末倒置。
    """
    try:
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        return (settings.export_dir / name).open("wb")
    except OSError:  # pragma: no cover - 磁盘异常
        logger.warning("导出文件落盘失败（不影响本次下载）", exc_info=True)
        return None


def _file_chunks(path: Path, *, delete: bool) -> Iterator[bytes]:
    """回放一个已落盘文件；`delete=True` 时读毕（或客户端中断）即删。"""
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_XLSX_STREAM_CHUNK)
                if not chunk:
                    break
                yield chunk
    finally:
        if delete:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                logger.warning("临时导出文件删除失败：%s", path, exc_info=True)


# =============================================================================
# 辅助
# =============================================================================
def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - 列内容损坏
        logger.warning("metrics JSON 列解析失败，按空处理", exc_info=True)
        return None


def _num(v: Any) -> float | None:
    """NaN / inf → None（`API-14-2`）。

    🔒 这是极易踩的坑：Python 的 `float('nan')` 经 FastAPI 序列化会产出
       `NaN` 字面量，**不是合法 JSON**，前端 `JSON.parse` 直接抛错，
       整个结果页白屏。实测 L2 的 MAPE 就常为 NaN（测试段缺可靠真值）。
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _round(v: float) -> float:
    """曲线/明细数值统一保留 2 位小数。

    ⚠️ 只用于**展示值**（曲线点、明细行、导出单元格），
       **不用于** `metrics` 各项 —— 指标必须原样透出（见 `_metric_bundle`）。
    """
    return round(float(v), 2)
