"""
=============================================================================
预测任务编排服务（`EP-5` · `WS-5-02` ~ `WS-5-07`）
-----------------------------------------------------------------------------
版本     : V1.0（EP-5）
依据文档 : 《开发技术文档 V1.0》§4.8（预测任务编排实现）、§4.7（算法层五条铁律）、
           §5.6（`API-12` / `API-13`）、`BE-16`（超时软失败）、`BE-24`（回测口径）
           《PRD V1.0》`FR-MOD-07` ~ `FR-MOD-13`、`NFR-PERF-01`、`NFR-REL-02/03/04`
           《项目开发实施方案 V1.0》`WS-5-02` ~ `WS-5-07`、`DOD-EP5`
-----------------------------------------------------------------------------
本模块是整个后端最核心的流程：把「一份数据集 + 一组参数」变成
「一批落库的预测值 + 一组可信的评估指标」。所有计算都委托给 `app/algo/`，
本模块只负责**编排、计时、容错、落库**。

🔒 架构红线：本模块**不得** `import algo.train`
   （`FR-MOD-07`「请求内不执行任何训练」/ `DOD-EP5` 红线自检）。
   允许 import `algo.infer` / `algo.classify` / `algo.metrics` 等推理与评估模块。
   守卫：`scripts/verify_ep5.py` 用 AST 取真实 import 做断言。

🔴 为什么要跑**两遍** `run_inference`（本阶段最重要的设计结论）
-----------------------------------------------------------------------------
`algo.infer.run_inference` 的 `backtest` 开关在两侧各有一个缺陷，实测如下：

| 模式 | `metrics` | `records[*].dates` |
|---|---|---|
| `backtest=True`  | ✅ 正确（样本外、与 `eval_report` 同口径） | ❌ 错位 horizon 天：模型只用「末尾前 T-14 天」的历史推理，`dates` 却写成「完整数据末尾之后」 |
| `backtest=False` | ❌ 无意义：预测的是**真实末尾之后**，而 `_to_metric_records` 拿**真实末尾之前**的 horizon 天当真值，**日期完全错位**（只是长度巧合相同） | ✅ 正确（真正的未来日期） |

用户对一次任务有**两个需求**，且分别落在上表的两列：
  ① 「未来 14 天会卖多少」 → 需要 `backtest=False` 的 `records`
  ② 「这个模型在我这份数据上有多准」 → 需要 `backtest=True` 的 `metrics`

故：**跑两遍，各取所需，绝不交叉使用**。耗时实测约 2×（本机 GPU 约 20 s），
仍远低于 60 s 硬约束（`NFR-PERF-01`）。

> ⚠️ 由此产生一项**已登记的偏离**：`DOD-EP5` 写「端到端 ≤ 60 s（实测目标 ≈ 8 s）」，
> 而本实现实测约 20 s。8 s 的假设是「一遍运行同时产出预测与指标」——
> 由上表可知那必然取到错位的数字。正确性是硬约束，8 s 只是目标参考值。
> 这一点同时解释了为什么 `DOD-EP6` 要求「结果页指标与 `eval_report.json`
> 逐项一致（14.26 / 26.42 / 27.52 / 83.95）」——只有回测那一遍才复现得出。

🔒 `BE-16` 超时走**软失败**，不返回 502
-----------------------------------------------------------------------------
同步执行的任务若被中间件强杀，会留下 `status=running` 的僵尸任务。故本模块
在每个阶段结束时检查 `deadline`，超预算则写 `status=failed` + `E_TIMEOUT` +
`completed_stage`，并**正常返回**（`code: 0`）—— 前端据此渲染失败态与处置建议。

🔒 `WS-5-06` GPU→CPU 回退**不是简单重试**
-----------------------------------------------------------------------------
必须三步齐全：捕获 OOM → `torch.cuda.empty_cache()` → **模型与输入都 `.to('cpu')`**。
只重试而不搬模型，会以同样的方式再 OOM 一次（本机实测过：
`Input and parameter tensors are not at the same device`）。
=============================================================================
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.algo import config as algo_config
from app.core import config
from app.core.errors import BizError, ErrorCode
from app.core.model_holder import ModelHandle, assert_ready
from app.dao import metric_dao, oplog_dao, prediction_dao, task_dao
from app.models.operation_log import ACTION_PREDICT, RESULT_FAIL, RESULT_SUCCESS
from app.models.task import STATUS_FAILED, STATUS_SUCCEEDED
from app.services import dataset_service
from app.validators import load_csv

logger = logging.getLogger(__name__)
settings = config.settings

__all__ = [
    "STAGES",
    "STAGE_TOTAL",
    "STAGE_LABELS",
    "STAGE_BUDGET_RATIO",
    "E_TIMEOUT",
    "E_INFER_FAIL",
    "E_TOO_MANY_SERIES",
    "create_and_run",
    "get_status",
    "progress_snapshot",
]

# =============================================================================
# 五阶段定义（`API-13` 的 `stage` 取值，唯一真源）
# =============================================================================
# ⚠️ 五个名字必须与 `code/backend/schemas.py::TaskStatusResponse`、前端
#    `types/index.ts::PipelineStage`、`STAGE_LABELS` 四处一致。
#    注意：UI/UX §8.3 的原型把阶段画成「数据预处理 / 特征构建 / **序列分级** /
#    批量模型推理 / 指标计算」，而接口契约是 `validate / preprocess / features /
#    inference / metrics`。「序列分级」在实现上归入 `features`（分级结果就是
#    特征构造的分流依据）。接口契约为准，原型只是文案差异。
STAGE_VALIDATE = "validate"
STAGE_PREPROCESS = "preprocess"
STAGE_FEATURES = "features"
STAGE_INFERENCE = "inference"
STAGE_METRICS = "metrics"

STAGES: tuple[str, ...] = (
    STAGE_VALIDATE,
    STAGE_PREPROCESS,
    STAGE_FEATURES,
    STAGE_INFERENCE,
    STAGE_METRICS,
)
STAGE_TOTAL = len(STAGES)

STAGE_LABELS: dict[str, str] = {
    STAGE_VALIDATE: "数据读取与校验",
    STAGE_PREPROCESS: "预处理 · 缺失插值 + 异常尖峰截断",
    STAGE_FEATURES: "特征构建 + 序列分级",
    STAGE_INFERENCE: "批量模型推理",
    STAGE_METRICS: "指标计算",
}

# ⏱️ 阶段**预算占比**（用于执行页时间轴的着色宽度）。
#
# 来源：EP-5 实测标定（113 序列 / horizon=14 / 两遍流水线，`scripts/_ep5_calibrate.py`）
#   validate   = 读取数据集                     31 ms
#   preprocess = 预处理 × 2 遍                 1,588 ms
#   features   = 特征构建 × 2 遍 + 序列分级     2,424 ms
#   inference  = 批量前向 × 2 + 滚动多步 × 2   8,610 ms   ← 61%
#   metrics    = 指标 × 2 遍                   1,450 ms
#   Σ = 14,103 ms（归一化后即下表）
#
# ⚠️ 推理段占绝对大头的原因，是它包含**逐序列逐日的滚动推理**
#    （81 序列 × 14 步 × 2 遍），而 `Timing.inference_ms` 只覆盖「一次批量前向」。
#    两者性质上同属推理，故合并计入 inference —— 这也是用户在 `UX-P07`
#    时间轴上应当看到的结论（「最贵的是推理」）。
#    → 守卫：`scripts/verify_ep5.py` 断言 inference 的占比是五者中**最大**的
#      （`UX-P07-01`），并断言五项之和 = 1.0；不比对魔数。
STAGE_BUDGET_RATIO: dict[str, float] = {
    STAGE_VALIDATE: 0.003,
    STAGE_PREPROCESS: 0.113,
    STAGE_FEATURES: 0.172,
    STAGE_INFERENCE: 0.610,
    STAGE_METRICS: 0.102,
}

# =============================================================================
# 任务级失败码（写入 `tasks.error_code`，`FR-SYS-02`）
# =============================================================================
# ⚠️ 口径澄清（已登记）：UI/UX §8.3 的失败卡把超时画成 `E_TIMEOUT（50001）`，
#    但技术文档 §5.2 的 500xx 表里 `50001 = INFER_FAIL`，而 `tasks.error_code`
#    是**字符串枚举**（`E_TIMEOUT / E_TOO_MANY_SERIES / E_INFER_FAIL`），
#    不是 400xx/500xx 业务码。故实现时以字符串枚举为准；
#    前端展示 `E_TIMEOUT`，不再把 50001 与它绑在一起（否则「超时」会被
#    误报成「推理失败」，处置建议也会给错）。
E_TIMEOUT = "E_TIMEOUT"
E_TOO_MANY_SERIES = "E_TOO_MANY_SERIES"
E_INFER_FAIL = "E_INFER_FAIL"

# 软失败的处置建议（`UX-P07-05` 要求三条：缩短步长 / 缩小商品范围 / 检查 CPU 回退）
_TIMEOUT_SUGGESTIONS: tuple[str, ...] = (
    "缩短预测步长：从 30 天降到 14 天可显著减少逐日推理量，这是最直接有效的降耗手段。",
    "缩小商品范围：按近 90 天销量取 Top N 商品重新上传，序列数下降后耗时近似线性下降。",
    "检查是否处于 CPU 回退状态：CPU 推理较 GPU 慢约 4.3 倍（本机实测）。"
    "若长期回退，需先解决显存占用问题。",
)

# 允许的预测步长。单一真源在 `algo.config.HORIZONS`（`TaskConfig` 与耗时模型也用它），
# 此处只取别名，避免三处各写一遍 (7, 14, 30) 后各自漂移。
_ALLOWED_HORIZONS: tuple[int, ...] = algo_config.HORIZONS

# 单条日志/说明的最大长度，避免把整表数据写进审计字段
_NOTE_MAX = 500


# =============================================================================
# 进程内进度登记（`API-13` 的真实来源）
# =============================================================================
@dataclass
class _Progress:
    """一次任务执行中的实时状态。

    ⚠️ 为什么放进程内内存而不是数据库：
       五阶段的推进是**毫秒~秒级**的高频事件，每次写库在 WAL 下都要 fsync，
       等于给 60 s 预算凭空加一笔开销。而 SQLite 单文件部署意味着
       进程重启 = 服务重启，内存态丢失不会比数据库态丢失更糟
       （此时 `API-13` 会从 DB 的 task 行降级推断出终态，见 `_from_db`）。
    """

    task_id: int
    stage: str = STAGE_VALIDATE
    stage_index: int = 1
    progress_pct: float = 0.0
    elapsed_ms: int = 0
    device: str = "cpu"
    device_fallback: int = 0
    series_count: int = 0
    l1_count: int | None = None
    l2_count: int | None = None
    l3_count: int | None = None
    finished: bool = False
    log: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "progress_pct": self.progress_pct,
            "elapsed_ms": self.elapsed_ms,
            "device": self.device,
            "device_fallback": self.device_fallback,
            "series_count": self.series_count,
            "l1_count": self.l1_count,
            "l2_count": self.l2_count,
            "l3_count": self.l3_count,
        }


_PROGRESS: dict[int, _Progress] = {}
_PROGRESS_LOCK = threading.Lock()
# 只保留最近 N 个任务的进度，避免长时间运行后内存无界增长
_PROGRESS_KEEP = 64


def _push_progress(prog: _Progress) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS[prog.task_id] = prog
        if len(_PROGRESS) > _PROGRESS_KEEP:
            for key in sorted(_PROGRESS)[: len(_PROGRESS) - _PROGRESS_KEEP]:
                if _PROGRESS[key].finished:
                    _PROGRESS.pop(key, None)


def _log_line(prog: _Progress, stage: str, message: str, level: str = "info") -> None:
    """记录一行执行摘要（`NFR-SEC-09`：只记摘要，绝不记销量明细）。"""
    prog.log.append({
        "stage": stage,
        "level": level,
        "message": message,
        "elapsed_ms": prog.elapsed_ms,
    })
    logger.info("[task %s] %s", prog.task_id, message)


def progress_snapshot(task_id: int) -> dict[str, Any] | None:
    with _PROGRESS_LOCK:
        prog = _PROGRESS.get(task_id)
    return prog.as_dict() if prog is not None else None


def _advance(prog: _Progress, stage: str, elapsed_ms: int) -> None:
    """推进到下一阶段。

    `progress_pct` 用**均分刻度**（20% / 阶段）而不是「按耗时加权」：
    本模块在同步阻塞模式下拿不到阶段内的细粒度回调，任何"更精细"的百分比
    都只能靠猜 —— 而编造的进度比粗粒度的真实进度更糟。
    「哪一段最贵」由前端的**时间轴宽度**（`STAGE_BUDGET_RATIO`）去表达。
    """
    idx = STAGES.index(stage) + 1
    prog.stage = stage
    prog.stage_index = idx
    prog.progress_pct = round(idx / STAGE_TOTAL * 100, 1)
    prog.elapsed_ms = elapsed_ms
    prog.updated_at = time.time()


# =============================================================================
# 主入口：`API-12`
# =============================================================================
def create_and_run(
    db: Session,
    *,
    user_id: int,
    dataset_id: int,
    horizon: int,
    use_holiday_feature: int = 1,
    task_name: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """创建并**同步执行**一个预测任务（`API-12`，`FR-MOD-07` ~ `13`）。

    返回 `CreateTaskResponse` 的载荷（dict）：
        {task_id, status, model_version, series_count, estimated_ms, ...}

    🔒 **顺序不可调整**：参数校验 → 归属校验 → 序列数预检 → 模型就绪 →
       **建任务落 running** → 执行。理由（`API-12-1`）：序列数超限时
       任务**不创建**，避免库里堆积一批注定失败的任务记录；
       而一旦开始执行，就必须先有任务行，否则异常时无处记录（`BE-16`）。
    """
    t_all = time.perf_counter()
    t_start = datetime.now()

    # ---------- 1. 参数校验（`40012`）----------
    if horizon not in _ALLOWED_HORIZONS:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"预测步长仅支持 {' / '.join(str(h) for h in _ALLOWED_HORIZONS)} 天，收到 {horizon}",
            {"field": "horizon", "allowed": list(_ALLOWED_HORIZONS)},
        )
    if use_holiday_feature not in (0, 1):
        raise BizError(
            ErrorCode.BAD_PARAM,
            "use_holiday_feature 仅接受 0 或 1",
            {"field": "use_holiday_feature", "allowed": [0, 1]},
        )

    # ---------- 2. 数据集归属（`BE-41` 两分支顺序不可交换）----------
    dataset = dataset_service.readable_or_404(db, dataset_id, user_id)

    # ---------- 3. 序列数预检（**前置于落库**，`API-12-1`）----------
    series_count = _resolve_series_count(dataset)
    if series_count > settings.max_series:
        raise BizError(
            ErrorCode.SERIES_OVER_LIMIT,
            f"待预测序列数为 {series_count:,}，超过上限 {settings.max_series:,}，"
            f"请先筛选商品（按近 90 天销量取 Top N）后重新上传",
            {
                "series_count": series_count,
                "series_limit": settings.max_series,
                "over_by": series_count - settings.max_series,
            },
        )

    # ---------- 4. 模型就绪（`40404`）----------
    # ⚠️ 必须在建任务**之前**：否则库里会留下一批「模型没就绪」的失败任务
    handle: ModelHandle = assert_ready()

    # ---------- 5. 建任务并**立刻提交**（`BE-16`：异常时要有记录可查）----------
    # 🔒 `core.deps.get_db` 对任何异常（含 BizError）都会 rollback，
    #    故此处必须显式 commit —— 与 `EP-4` 踩过的是同一个坑
    #    （「复现得了但查不到」）。
    task = task_dao.create(
        db,
        user_id=user_id,
        dataset_id=dataset.id,
        horizon=horizon,
        model_version=handle.version,
        task_name=_clean_task_name(task_name, dataset, horizon),
        use_holiday_feature=use_holiday_feature,
    )
    task_dao.mark_running(
        db, task.id,
        started_at=t_start.strftime("%Y-%m-%d %H:%M:%S"),
        device=handle.device,
        device_fallback=0,
        series_total=series_count,
    )
    db.commit()

    prog = _Progress(task_id=task.id, device=handle.device, series_count=series_count)
    _push_progress(prog)
    _log_line(prog, STAGE_VALIDATE, f"task {task.id} created · horizon={horizon} · "
                                    f"series={series_count} · model={handle.version}")

    deadline = t_all + settings.task_budget_ms / 1000.0

    try:
        payload = _execute(
            db, task=task, dataset=dataset, handle=handle, prog=prog,
            horizon=horizon, use_holiday_feature=use_holiday_feature,
            deadline=deadline, started=t_all, user_id=user_id,
        )
    except BizError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底转软失败，绝不让原始 500 外泄
        logger.exception("预测任务执行失败 task_id=%s", task.id)
        payload = _fail(
            db, task=task, prog=prog, error_code=E_INFER_FAIL,
            error_msg=f"推理过程异常：{type(exc).__name__}",
            completed_stage=prog.stage, duration_ms=int((time.perf_counter() - t_all) * 1000),
            user_id=user_id, ip=ip,
        )
    return payload


def _budget_text() -> str:
    """预算的人类可读文本。

    ⚠️ 不直接写 `task_budget_ms // 1000`：整秒表达式在**小于 1 秒的测试配置**下
       会退化成「超过 0 秒预算」—— 一条自相矛盾的提示。而验证软失败的最省事办法
       恰恰就是把预算配成 1 ms（避免真的等 60 秒），所以这条路径一定会被走到，
       不能留一个「只在测试时才出现的怪文案」。
    """
    ms = settings.task_budget_ms
    if ms >= 1000 and ms % 1000 == 0:
        return f"{ms // 1000} 秒"
    return f"{ms} 毫秒"


# =============================================================================
# 执行主体
# =============================================================================
def _execute(
    db: Session,
    *,
    task: Any,
    dataset: Any,
    handle: ModelHandle,
    prog: _Progress,
    horizon: int,
    use_holiday_feature: int,
    deadline: float,
    started: float,
    user_id: int,
) -> dict[str, Any]:
    from app.algo import infer as algo_infer

    # ---------- 阶段 1：读取 + 校验 ----------
    t0 = time.perf_counter()
    df = _read_dataset(dataset)
    read_ms = int((time.perf_counter() - t0) * 1000)
    _advance(prog, STAGE_VALIDATE, int((time.perf_counter() - started) * 1000))
    _log_line(prog, STAGE_VALIDATE, f"dataset loaded · rows={len(df):,} · {read_ms} ms")

    # ---------- 阶段 2~5：两遍流水线 ----------
    # 第 1 遍 backtest=True：只取 metrics / coverage / distribution（评估用）
    # 第 2 遍 backtest=False：只取 records（未来预测值）
    run = _run_pipeline(
        algo_infer, df, handle, horizon=horizon,
        holiday=bool(use_holiday_feature), prog=prog, deadline=deadline,
        started=started,
    )
    back, fore = run["back"], run["fore"]

    # ---------- 超预算软失败（`BE-16` / `API-12-2`）----------
    if time.perf_counter() > deadline:
        return _fail(
            db, task=task, prog=prog, error_code=E_TIMEOUT,
            error_msg=(
                f"端到端耗时超过预算 {_budget_text()}"
                f"（已完成阶段：{STAGE_LABELS.get(prog.stage, prog.stage)}）"
            ),
            completed_stage=prog.stage,
            duration_ms=int((time.perf_counter() - started) * 1000),
            user_id=user_id, extra={"suggestions": list(_TIMEOUT_SUGGESTIONS)},
        )

    # ---------- 落库：predictions（批量，`NFR-REL-04`）----------
    rows = _build_prediction_rows(task.id, fore, df, horizon)
    written = prediction_dao.bulk_insert(db, rows)

    # ---------- 落库：metrics ----------
    metrics = back.metrics
    cov = back.coverage
    timing_json = _timing_payload(back, fore, read_ms, handle, prog)
    metric_dao.create(
        db,
        task_id=task.id,
        mape=_f(metrics.weighted_mape),
        mae=_f(metrics.weighted_mae),
        rmse=_f(metrics.weighted_rmse),
        baseline_mape=None,                # 预留列恒 NULL（TD-DB-04）
        baseline_mae=_f(metrics.baseline_mae),
        baseline_rmse=_f(metrics.baseline_rmse),
        mae_improve_pct=_f(metrics.mae_improve_pct),
        rmse_improve_pct=_f(metrics.rmse_improve_pct),
        series_evaluated=metrics.series_evaluated,
        series_passed=metrics.series_passed,
        series_pass_rate=_f(metrics.series_pass_rate),
        l1_mape=_f(metrics.l1_mape),
        # ⚠️ L2 常为 NaN（测试段缺真值），必须转 None 再落库，
        #    否则 JSON 序列化会得到 NaN —— 前端 JSON.parse 直接抛错（API-14-2 / TQ-16）
        l2_mape=_f(metrics.l2_mape),
        is_over_threshold=0 if metrics.mape_ok else 1,
        eval_note=_eval_note(handle, horizon, cov),
        distribution_json=json.dumps(_distribution(back.distribution), ensure_ascii=False),
        timing_json=json.dumps(timing_json, ensure_ascii=False),
    )

    duration_ms = int((time.perf_counter() - started) * 1000)
    task_dao.mark_succeeded(
        db, task.id,
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        duration_ms=duration_ms,
        l1_count=cov["L1"], l2_count=cov["L2"], l3_count=cov["L3"],
        # 🔒 必须落库（`EP-9` 修复的真实缺陷）：GPU OOM → CPU 重跑成功后，
        #    只写内存态会让 `tasks` 行停在 `device='cuda', device_fallback=0`，
        #    重启或从历史列表重开该任务时降级事实即丢失（`NFR-REL-03` / §9.4 B-17）。
        device=prog.device or None,
        device_fallback=int(prog.device_fallback or 0),
    )
    oplog_dao.record(
        db, action=ACTION_PREDICT, result=RESULT_SUCCESS, user_id=user_id,
        target_id=task.id,
        detail=(f"dataset={dataset.id};horizon={horizon};series={cov['total']};"
                f"l1={cov['L1']};l2={cov['L2']};l3={cov['L3']};"
                f"mape={metrics.weighted_mape:.2f};rows={written};ms={duration_ms}"),
        ip=None,
    )
    db.commit()

    prog.finished = True
    prog.l1_count, prog.l2_count, prog.l3_count = cov["L1"], cov["L2"], cov["L3"]
    prog.progress_pct = 100.0
    prog.elapsed_ms = duration_ms
    prog.stage = STAGE_METRICS
    prog.stage_index = STAGE_TOTAL
    _push_progress(prog)
    _log_line(prog, STAGE_METRICS,
              f"done · rows={written} · mape={metrics.weighted_mape:.2f}% · {duration_ms} ms")

    # ⏱️ 写库本身也要算进端到端（`NFR-PERF-01` 计的是用户等待时间）
    return {
        "task_id": task.id,
        "status": STATUS_SUCCEEDED,
        "model_version": handle.version,
        "series_count": cov["total"],
        "estimated_ms": duration_ms,
        "device": handle.device,
        "device_fallback": prog.device_fallback,
        "coverage": cov,
        "mape": _f(metrics.weighted_mape),
        "duration_ms": duration_ms,
        # 真实分段耗时随响应回带：同步 API-12 下前端运行期间拿不到 task_id，
        # 无法轮询 API-13，若不回带则执行页永远只能显示"预估"而非"实测"。
        "timing": timing_json,
    }


def _run_pipeline(
    algo_infer: Any,
    df: pd.DataFrame,
    handle: ModelHandle,
    *,
    horizon: int,
    holiday: bool,
    prog: _Progress,
    deadline: float,
    started: float,
) -> dict[str, Any]:
    """两遍流水线 + GPU OOM 回退（`WS-5-05` / `WS-5-06`）。

    🔒 `WS-5-05` 禁止逐序列循环调用模型：本函数**只调 `run_inference`**，
       批量前向在 `algo.infer` 内部完成（`preds = model(batch)`）。
       本模块不出现任何 `for ... model(...)` 的逐序列调用。
    """

    def _once(device: str) -> tuple[Any, Any]:
        common = dict(
            horizon=horizon,
            model=handle.model,
            feat_names=handle.feat_names or None,
            window=handle.window,
            holiday=holiday,
            device=device,
        )
        back = algo_infer.run_inference(df, backtest=True, **common)
        fore = algo_infer.run_inference(df, backtest=False, **common)
        return back, fore

    device = handle.device
    fallback = 0
    _advance(prog, STAGE_PREPROCESS, int((time.perf_counter() - started) * 1000))
    _log_line(prog, STAGE_PREPROCESS,
              f"pipeline start · backtest + forecast 两遍 · device={device}")

    try:
        back, fore = _once(device)
    except Exception as exc:  # noqa: BLE001 - 只有 OOM 才回退，其余照抛
        if not _is_oom(exc):
            raise
        # 🔒 WS-5-06：三步齐全，缺一不可 ——
        #    ① 清空缓存 ② 模型搬到 CPU ③ 输入也搬到 CPU（device 参数即输入设备）
        #    只做「重试」会以同样的方式再 OOM 一次。
        device = "cpu"
        fallback = 1
        _recover_from_oom(handle)
        _log_line(prog, STAGE_INFERENCE,
                  "CUDA out of memory → 已回退 CPU 重跑（任务不会失败，耗时预计增加）",
                  level="warning")
        # ⚠️ 记在**重跑之前**：若 CPU 重跑也抛异常，兜底异常出口拿到的
        #    `prog` 才是"已降级"的真实状态；写在重跑之后会得到
        #    「在 cuda 上失败了」这种与事实相反的记录。
        prog.device, prog.device_fallback = device, fallback
        back, fore = _once(device)

    prog.device = device
    prog.device_fallback = fallback
    _advance(prog, STAGE_FEATURES, int((time.perf_counter() - started) * 1000))
    _log_line(prog, STAGE_FEATURES,
              f"features built · series={back.coverage['total']} · "
              f"L1={back.coverage['L1']} L2={back.coverage['L2']} L3={back.coverage['L3']}")
    _advance(prog, STAGE_INFERENCE, int((time.perf_counter() - started) * 1000))
    _log_line(prog, STAGE_INFERENCE,
              f"batch forward · batch_size={back.coverage['L1']} · "
              f"{back.timing.inference_ms} ms（一次前向，不逐序列循环）")
    _advance(prog, STAGE_METRICS, int((time.perf_counter() - started) * 1000))
    _log_line(prog, STAGE_METRICS,
              f"metrics · mape={back.metrics.weighted_mape:.2f}% · "
              f"pass_rate={back.metrics.series_pass_rate:.2f}%")
    return {"back": back, "fore": fore, "device": device, "device_fallback": fallback,
            "deadline_hit": time.perf_counter() > deadline}


def _is_oom(exc: BaseException) -> bool:
    """识别显存不足。

    ⚠️ 不能只捕 `torch.cuda.OutOfMemoryError`：torch 在不同版本/不同触发路径下
       既有专用异常也有 `RuntimeError: CUDA out of memory...`，
       只捕前者会漏掉后者（实测本机的 `Input and parameter tensors are not at
       the same device` 就是 RuntimeError，必须让它冒泡而不是被当成 OOM）。
    """
    text = str(exc).lower()
    return "out of memory" in text or "cuda error" in text and "memory" in text


def _recover_from_oom(handle: ModelHandle) -> None:
    """OOM 后的三件事：清缓存 → 模型搬到 CPU → 记账。"""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # pragma: no cover - 无 CUDA 环境
        logger.debug("empty_cache 跳过（无可用 CUDA）", exc_info=True)
    # 🔒 模型必须一起搬：只把输入搬到 CPU 会再次 OOM/报设备不一致
    try:
        handle.model.to("cpu")
    except Exception:  # pragma: no cover
        logger.warning("模型搬迁到 CPU 失败，回退可能仍会失败", exc_info=True)
    handle.device = "cpu"


# =============================================================================
# 失败出口（软失败，`BE-16`）
# =============================================================================
def _fail(
    db: Session,
    *,
    task: Any,
    prog: _Progress,
    error_code: str,
    error_msg: str,
    completed_stage: str | None,
    duration_ms: int,
    user_id: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """标记任务失败并**正常返回**（`code: 0`）。

    🔒 `API-12-2`：超预算**不得**返回 HTTP 502/504。这里返回的是一个
       `code: 0` 的正常响应，任务状态由 `status`/`error_code` 表达。
       前端据此渲染失败卡与三条处置建议。
    """
    task_dao.mark_failed(
        db, task.id,
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        error_code=error_code,
        error_msg=_trim(error_msg),
        completed_stage=completed_stage,
        duration_ms=duration_ms,
        # 🔒 失败也要把设备与回退标记写下去（`EP-9` 修复）：
        #    「OOM 回退 CPU 之后仍然超预算」是最需要被运维看见的组合，
        #    若只在成功路径记账，这种情况会显示成「在 cuda 上超时」，与事实相反。
        device=prog.device or None,
        device_fallback=int(prog.device_fallback or 0),
    )
    oplog_dao.record(
        db, action=ACTION_PREDICT, result=RESULT_FAIL, user_id=user_id,
        target_id=task.id,
        detail=f"dataset={task.dataset_id};code={error_code};"
               f"stage={completed_stage};ms={duration_ms}",
        ip=None,
    )
    db.commit()

    prog.finished = True
    prog.elapsed_ms = duration_ms
    _push_progress(prog)
    _log_line(prog, prog.stage, f"failed · {error_code} · {error_msg}", level="error")

    payload: dict[str, Any] = {
        "task_id": task.id,
        "status": STATUS_FAILED,
        "model_version": task.model_version,
        "series_count": prog.series_count,
        "estimated_ms": duration_ms,
        "device": prog.device,
        "device_fallback": prog.device_fallback,
        "error_code": error_code,
        "error_msg": _trim(error_msg),
        "completed_stage": completed_stage,
        "duration_ms": duration_ms,
    }
    if extra:
        payload.update(extra)
    return payload


# =============================================================================
# `API-13` 任务状态
# =============================================================================
def get_status(db: Session, *, task_id: int, user_id: int) -> dict[str, Any]:
    """查询任务状态（`API-13`）。

    进度优先取进程内登记；若没有（进程重启过、或任务由别的 worker 跑完），
    则从 `tasks` 行**降级推断**：终态 → 100%，未完成 → 退回初始态。
    这条降级路径很重要：让 `API-13` 在「内存态丢失」时仍给出**正确且不撒谎**
    的答案，而不是报 404。

    🔒 归属判定（`NFR-SEC-05` PRD P0）：**越权 → `40301`**（任务存在但不属于
    调用者），**不存在 / 已软删除 → `40401`**。两码必须可区分，
    且与 `result_service` / `history_service` / `dataset_service` 对齐。
    """
    task = task_dao.get_by_id(db, task_id)
    if task is None or int(task.is_deleted or 0) == 1:
        raise BizError(
            ErrorCode.TASK_NOT_FOUND, "任务不存在或无权查看",
            {"task_id": task_id},
        )
    if int(task.user_id or 0) != int(user_id):
        # 🔒 `NFR-SEC-05`（PRD P0）：**越权返回 `40301` 而非 `40401`**。
        #    `EP-9` 校验链回归（`verify_ep5_api` ⑨）发现本接口原用
        #    `task_dao.get_owned` 把「不存在」与「越权」合并成同一个 `40401`，
        #    与 `result_service._owned_task`、`history_service.soft_delete`、
        #    `dataset_service` 三处的 `40301` 口径不一致 —— 同一个「别人的资源」，
        #    有的接口说 403、有的说 404，前端无法统一处理，
        #    也违背 PRD 措辞「越权返回 403 而非 404（避免资源探测）」。
        #    ⚠️ 三步顺序不可颠倒：`40401` 只留给「真的不存在」与「已软删除」，
        #       否则用户删完任务再点旧链接会被告知「无权访问」，反而误导。
        raise BizError(
            ErrorCode.FORBIDDEN, "无权查看该任务",
            {"task_id": task_id},
        )

    prog = _PROGRESS.get(task_id)
    if prog is not None:
        data = prog.as_dict()
        # 内存态可能落后于已提交的终态（例如另一进程跑完），以 DB 为准修正
        if task.status in (STATUS_SUCCEEDED, STATUS_FAILED) and not prog.finished:
            data["progress_pct"] = 100.0
        return _merge_task(db, task, data)

    return _merge_task(db, task, _from_db(task))


def _from_db(task: Any) -> dict[str, Any]:
    """无内存态时的降级推断（绝不编造中间进度）。"""
    done = task.status in (STATUS_SUCCEEDED, STATUS_FAILED)
    return {
        "task_id": task.id,
        "stage": STAGE_METRICS if done else STAGE_VALIDATE,
        "stage_index": STAGE_TOTAL if done else 1,
        "progress_pct": 100.0 if done else 0.0,
        "elapsed_ms": int(task.duration_ms or 0),
        "device": task.device or "cpu",
        "device_fallback": int(task.device_fallback or 0),
        "series_count": int(task.series_total or 0),
        "l1_count": task.l1_count,
        "l2_count": task.l2_count,
        "l3_count": task.l3_count,
    }


def _merge_task(db: Session, task: Any, progress: dict[str, Any]) -> dict[str, Any]:
    """把 DB 的权威字段覆盖到进度快照上，拼成 `TaskStatusResponse` 的形状。

    🔒 权威性分工：`status` / `error_*` / `device*` / 三个分级计数一律**以 DB 为准**；
       `stage` / `progress_pct` / `elapsed_ms` 才用内存态。
       反过来会出现「任务已 succeeded，进度条还停在 40%」这类自相矛盾的展示。
    """
    device = task.device or progress.get("device") or "cpu"
    return {
        "task_id": task.id,
        "status": task.status,
        "stage": progress["stage"],
        "stage_index": int(progress["stage_index"]),
        "stage_total": STAGE_TOTAL,
        "progress_pct": float(progress["progress_pct"]),
        "device": device if device in ("cuda", "cpu") else "cpu",
        "device_fallback": int(task.device_fallback or progress.get("device_fallback") or 0),
        "elapsed_ms": int(progress.get("elapsed_ms") or task.duration_ms or 0),
        "budget_ms": settings.task_budget_ms,
        "series_count": int(task.series_total or progress.get("series_count") or 0),
        "l1_count": task.l1_count if task.l1_count is not None else progress.get("l1_count"),
        "l2_count": task.l2_count if task.l2_count is not None else progress.get("l2_count"),
        "l3_count": task.l3_count if task.l3_count is not None else progress.get("l3_count"),
        "error_code": task.error_code,
        "error_msg": task.error_msg,
        "completed_stage": task.completed_stage,
    }


# =============================================================================
# 数据读取与行构造
# =============================================================================
def _read_dataset(dataset: Any) -> pd.DataFrame:
    """读取数据集 CSV（编码回退 + 日期解析与上传阶段同源）。"""
    path = dataset_service._abs(dataset)  # noqa: SLF001 - 同包内的路径还原工具
    if not path.exists():
        raise BizError(
            ErrorCode.DATASET_NOT_FOUND,
            "数据集文件已不存在，请重新上传",
            {"dataset_id": dataset.id, "file_path": dataset.file_path},
        )
    df, _enc = load_csv(path, None)
    return df


def _build_prediction_rows(
    task_id: int, result: Any, df: pd.DataFrame, horizon: int
) -> list[dict[str, Any]]:
    """把 `InferenceResult.records` 转成 `predictions` 表行（批量落库用）。

    🔒 三条口径必须守住：

    1. **L3 也落一行，但值是历史均值而不是预测值**
       `predictions.predicted_value` 是 NOT NULL，而 `FR-MOD-08` 规定
       「L3 输出**历史均值**并标记不可预测」。故 L3 的 `predicted_value`
       = 历史均值，`granularity_level='L3'`；结果页据该列判定为
       「不显示数值预测，只显示降级说明」（`UX-AC-10`）。
       若这里不落行，`prediction_dao.count_by_level` 的 L3 会恒为 0，
       覆盖率披露（`FR-VIS-07`）就永远看不到 L3 —— 那才是真正误导用户。

    2. **`baseline_value` 用「未来窗口的移动平均」**
       两遍运行里第二遍（`backtest=False`）的 `metric_records` 里的 baseline
       是**过去**窗口的基线（日期错位），不能用。这里对每条序列用**全部历史**
       外推 horizon 天，与「预测未来」的语义对齐。

    3. **`actual_value` 恒为 NULL**
       在线预测的真值尚未发生（`prediction.py` 的列注释即如此）。回测的真值
       只存在于第 1 遍的 `metric_records` 中，且**不写本表** —— 否则结果页的
       明细表会把「过去的真实值」和「未来的预测值」混在同一列日期里。
    """
    from app.algo import config as algo_config
    from app.algo import fallback as algo_fallback
    from app.algo import metrics as algo_metrics

    rows: list[dict[str, Any]] = []
    horizon_dates = _horizon_dates(df, horizon)

    for rec in result.records:
        if rec.level == "L3" or not rec.forecast:
            # L3：只落一行历史均值（占位 + 降级标记），不参与数值预测展示
            _none, note = algo_fallback.l3_forecast(df, rec.sku, rec.warehouse)
            mean = note.get("historical_mean")
            rows.append({
                "task_id": task_id,
                "sku_id": rec.sku,
                "warehouse_id": rec.warehouse,
                "forecast_date": horizon_dates[0],
                "predicted_value": float(mean) if mean is not None and not _nan(mean) else 0.0,
                "actual_value": None,
                "baseline_value": None,
                "granularity_level": "L3",
                "lower_bound": None,
                "upper_bound": None,
            })
            continue

        vals = _series_values(df, rec.sku, rec.warehouse)
        base = algo_metrics.moving_average_forecast(
            vals, horizon, algo_config.BASELINE_WINDOW
        )
        lower = rec.lower or []
        upper = rec.upper or []
        for i, value in enumerate(rec.forecast[:horizon]):
            date = rec.dates[i] if i < len(rec.dates) else horizon_dates[i]
            rows.append({
                "task_id": task_id,
                "sku_id": rec.sku,
                "warehouse_id": rec.warehouse,
                "forecast_date": date,
                "predicted_value": float(value),
                "actual_value": None,
                "baseline_value": float(base[i]) if i < len(base) else None,
                "granularity_level": rec.level,
                "lower_bound": float(lower[i]) if i < len(lower) else None,
                "upper_bound": float(upper[i]) if i < len(upper) else None,
            })
    return rows


def _horizon_dates(df: pd.DataFrame, horizon: int) -> list[str]:
    """全表统一的未来窗口（与 `algo.infer._future_dates` 同口径：数据末尾之后）。"""
    from app.algo import config as algo_config

    last = pd.to_datetime(df[algo_config.COL_DATE]).max()
    return [(last + pd.Timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(horizon)]


def _series_values(df: pd.DataFrame, sku: str, warehouse: str) -> Any:
    """某条序列的有效历史（去 NaN），用于算未来窗口的移动平均基线。"""
    import numpy as np

    from app.algo import config as algo_config

    sub = df[
        (df[algo_config.COL_SKU] == sku) & (df[algo_config.COL_WAREHOUSE] == warehouse)
    ].sort_values(algo_config.COL_DATE)
    vals = sub[algo_config.COL_SALES].to_numpy(dtype=float)
    return vals[~np.isnan(vals)]


# =============================================================================
# 辅助
# =============================================================================
def _resolve_series_count(dataset: Any) -> int:
    """待预测序列数。

    优先用上传时算好的 `valid_series_count`（那是同一次分级的产物，最准也最省）。
    只有它**缺失或为 0** 时才回退重算 —— 注意不能把 0 直接当成「没有序列」，
    否则超限预检（`API-12-1`）会静默失效：明明有 5000 条序列却判定 0 ≤ 1000 放行，
    然后真的开始跑，白白吃掉整整一分钟预算。
    """
    count = int(dataset.valid_series_count or 0)
    if count > 0:
        return count
    try:
        df = _read_dataset(dataset)
        from app.algo import classify as algo_classify

        return int(algo_classify.estimate_series_count(df))
    except Exception:  # pragma: no cover - 读取失败时交给下游报错
        logger.warning("序列数回算失败，沿用 %s", count, exc_info=True)
        return count


def _clean_task_name(raw: str | None, dataset: Any, horizon: int) -> str | None:
    """任务名清洗：去空白、截断、为空时按数据集与步长生成。

    ⚠️ 截断按**字符**（不是字节）：`schemas.py` 的 `max_length=64` 是 pydantic 的
       字符语义，按字节截断会让中文名被砍到 21 字，与契约不符。
    """
    name = (raw or "").strip()
    if name:
        return name[:64]
    stem = Path(str(dataset.file_name or "dataset")).stem
    return f"{stem} · {horizon} 天预测"[:64]


def _eval_note(handle: ModelHandle, horizon: int, cov: dict[str, Any]) -> str:
    """评估前提（`FR-MOD-13` 强制留痕）。

    需求原文要求写明「使用预训练模型，未针对本次输入数据微调」，并要求
    结果页与导出文件可见。这里把**关键前提一次性说全**：
    模型版本、步长、评估方式、序列构成、以及为什么不是全部序列都参与指标。
    """
    text = (
        f"使用预训练模型 {handle.version}，未针对本次输入数据微调（FR-MOD-13）。"
        f"评估方式：把每条序列末尾 {horizon} 天作为留出集做样本外回测，"
        f"模型只使用其之前的历史。"
        f"主指标（加权 MAPE / MAE / RMSE）只统计 L1 序列 —— "
        f"L2/L3 的误差由规则（商品级降级 / 历史均值）决定，不是 LSTM 的能力，"
        f"混入会把达标模型误判为不达标（BE-23）。"
        f"本次序列构成：L1 {cov['L1']} / L2 {cov['L2']} / L3 {cov['L3']}，"
        f"共 {cov['total']} 条。"
    )
    return _trim(text)


def _timing_payload(
    back: Any,
    fore: Any,
    read_ms: int,
    handle: ModelHandle,
    prog: _Progress,
) -> dict[str, Any]:
    """阶段耗时明细（落 `metrics.timing_json`，自由结构、不属接口契约）。

    ⚠️ 口径澄清（`EP-5` 修正了两处，都是本模块早期版本的错误）：

    ① **`algorithm_total_ms` 原先只算了一遍**。任务要跑两遍流水线
       （`backtest=True` 取评估指标 + `backtest=False` 取未来预测值），
       所以「本次任务在算法层花了多久」必须是两遍之和 —— 否则它会显著小于
       `wall_ms`，看上去像「凭空多出十几秒开销」，把真正的大头藏起来。

    ② **`rolling_ms` / `fallback_ms` 原先根本没有埋点**。`inference_ms` 只覆盖
       「一次批量前向」（实测个位数~百毫秒），而真实耗时的大头是**逐序列多步
       滚动**。缺了埋点就只能把十几秒的差额笼统写成 "unaccounted"，
       既看不出「哪一段最贵」，也无法判断优化有没有生效。
       ⇒ `EP-5` 已在 `algo.infer.Timing` 补上这两个字段，此处如实透出。

    ③ **`stages` 与 `stages_two_pass_ms` 是两套口径，不可混用**（`EP-5` 新增）：

       | 键 | 口径 | 用途 |
       |---|---|---|
       | `stages` | **单遍**（backtest 那一遍）的原始字段 | 与 `outputs/eval_report.json` 的 `timing_ms` 可比，是「模型评测」口径 |
       | `stages_two_pass_ms` | **两遍合计**，且推理段并入 `rolling_ms` | 前端执行页时间轴着色，是「本次任务花了多少」口径 |

       为什么必须有第二套：任务实际跑两遍，单遍数字只反映**一半**成本，用它画时间轴
       会让「哪一段最贵」失真；而 `inference_ms` 只覆盖「一次批量前向」（实测个位数
       毫秒），真正的大头是逐序列多步滚动 —— 与 `STAGE_BUDGET_RATIO` 的标定口径
       （推理并入滚动）保持一致。
    """
    tb, tf = back.timing, fore.timing
    stages = {
        STAGE_VALIDATE: read_ms,
        STAGE_PREPROCESS: tb.preprocess_ms,
        STAGE_FEATURES: tb.features_ms,
        STAGE_INFERENCE: tb.inference_ms,
        STAGE_METRICS: tb.metrics_ms,
    }
    # 两遍合计：读取只发生一次（故 validate 不乘 2），其余阶段逐遍相加；
    # 推理段并入 rolling_ms（见上文 ③）。
    stages_two = {
        STAGE_VALIDATE: read_ms,
        STAGE_PREPROCESS: tb.preprocess_ms + tf.preprocess_ms,
        STAGE_FEATURES: tb.features_ms + tf.features_ms,
        STAGE_INFERENCE: tb.inference_ms + tf.inference_ms + tb.rolling_ms + tf.rolling_ms,
        STAGE_METRICS: tb.metrics_ms + tf.metrics_ms,
    }
    algo_two = tb.total_ms + tf.total_ms
    inner_sum = sum(stages.values())
    return {
        "stages": stages,
        "stages_two_pass_ms": stages_two,
        "stages_two_pass_total_ms": sum(stages_two.values()),
        "stage_labels": STAGE_LABELS,
        "stage_budget_ratio": STAGE_BUDGET_RATIO,
        "inference_ms": tb.inference_ms,
        "rolling_ms": tb.rolling_ms,
        "fallback_ms": tb.fallback_ms,
        # 单遍内部、既不在五阶段里也不在 rolling/fallback 里的部分
        # （主要是序列分级 classify 与结果装配）
        "unaccounted_ms": max(tb.total_ms - inner_sum - tb.rolling_ms - tb.fallback_ms, 0),
        "unaccounted_note": (
            "序列分级（classify）+ 结果装配。滚动多步与 L2/L3 兜底已于 EP-5 单独埋点"
            "（rolling_ms / fallback_ms），不再并入此处。"
        ),
        "algorithm": {
            "backtest_pass_ms": tb.total_ms,
            "forecast_pass_ms": tf.total_ms,
        },
        "algorithm_total_ms": algo_two,
        # wall 与算法两遍之差 = 读取数据集 + 两遍之间的编排 + 分级/分发 + 批量落库
        "io_overhead_ms": max(prog.elapsed_ms - algo_two, 0),
        "wall_ms": prog.elapsed_ms,
        "device": prog.device,
        "device_fallback": prog.device_fallback,
        "passes": 2,
        "passes_note": (
            "两遍流水线：backtest=True 取评估指标（样本外），"
            "backtest=False 取未来预测值。二者不可合并，理由见 predict_service 模块 docstring。"
        ),
        "model_version": handle.version,
        "window": handle.window,
        "hidden_size": handle.hidden_size,
        "num_layers": handle.num_layers,
        "param_count": handle.param_count,
    }


def _distribution(raw: dict[str, Any]) -> dict[str, Any]:
    """逐序列 MAPE 分布（`FR-VIS-08` 直方图数据）。

    把 `algo.metrics.series_mape_distribution` 的原始值列表压成
    **20 个分箱**（每箱 5 个百分点）后再落库：原始列表最多 1000 个数，
    全量存进 `TEXT` 列既浪费又对展示无增益（直方图只需要计数）。
    """
    values = [float(v) for v in raw.get("values", []) if v is not None and not _nan(v)]
    buckets = [0] * 20
    for v in values:
        idx = min(int(v // 5), 19) if v >= 0 else 0
        buckets[idx] += 1
    return {
        "median": _f(raw.get("median")),
        "p75": _f(raw.get("p75")),
        "p90": _f(raw.get("p90")),
        "max": _f(raw.get("max")),
        "over_count": int(raw.get("over_count") or 0),
        "threshold": float(raw.get("threshold") or 20.0),
        "count": len(values),
        "bucket_size": 5,
        "buckets": buckets,
    }


def _nan(value: Any) -> bool:
    try:
        return value is None or value != value  # NaN 是唯一不等于自身的值
    except Exception:  # pragma: no cover
        return True


def _f(value: Any) -> float | None:
    """NaN / inf → None（JSON 与 SQLite REAL 都不能安全承载 NaN）。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v, 4)


def _trim(text: str) -> str:
    return text if len(text) <= _NOTE_MAX else text[: _NOTE_MAX - 1] + "…"
