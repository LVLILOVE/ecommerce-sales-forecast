"""在线主路径：加载已注册权重 → 批量推理 → 兜底 → 指标。

📌 这里是**唯一**被 services/ 调用的算法入口（PRD 数据流 2）。
   端到端预算 60 秒，各阶段耗时都要埋点（写入 task.duration_ms）。

为什么是「批量推理」而不是「逐序列循环」：
   50 条序列逐条调用模型，PyTorch 的 kernel 启动开销会占掉大部分时间；
   把同一批窗口堆成一个 batch 送进网络，耗时几乎是单条的 1–2 倍。
   ⏱ 本机实测：50 序列批量推理约 22s，逐条则超过 60s，直接击穿 SLA。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import torch

from . import config as C
from . import classify, fallback, features, metrics as metrics_mod, preprocess

logger = logging.getLogger(__name__)


@dataclass
class Timing:
    """分阶段耗时（毫秒）。用于运维定位瓶颈，也用于前端进度条。

    ⚠️ `inference_ms` 只覆盖**一次批量前向**（约 21 ms），这是「批量推理」这个
       设计决策的证据 —— 但真实任务的大头在**逐序列多步滚动**（113 × 14 次
       单样本前向），它既不属于「批量前向」也不属于「特征工程」。
       `EP-5` 之前这段完全没埋点，只能在 `total_ms - Σstages` 里看到一个
       十几秒的黑洞（`predict_service._timing_payload` 当时只能把它标成
       "unaccounted"）。`EP-5` 补上 `rolling_ms` 与 `fallback_ms` 两个字段，
       让「谁最贵」有据可查，也为下一步的向量化优化留下改进前的基线。
    """

    preprocess_ms: int = 0
    features_ms: int = 0
    inference_ms: int = 0
    rolling_ms: int = 0        # 逐序列多步滚动（EP-5 新增埋点）
    fallback_ms: int = 0       # L2 / L3 规则兜底（EP-5 新增埋点）
    metrics_ms: int = 0
    total_ms: int = 0

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class PredictionRecord:
    """单条序列的预测结果，直接映射 predictions 表。"""

    sku: str
    warehouse: str
    level: str
    dates: list[str]
    forecast: list[float] | None      # L3 为 None
    lower: list[float] | None = None
    upper: list[float] | None = None
    note: str = ""

    @property
    def predictable(self) -> bool:
        return self.forecast is not None


@dataclass
class InferenceResult:
    records: list[PredictionRecord] = field(default_factory=list)
    metrics: C.Metrics = field(default_factory=C.Metrics)
    coverage: dict = field(default_factory=dict)
    timing: Timing = field(default_factory=Timing)
    distribution: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    device: str = "cpu"
    # 逐序列指标明细（含 y_true / y_pred / baseline），供验收导出与原生对齐使用。
    # 与 metrics 同源，因此导出端不需要复刻任何计算逻辑。
    metric_records: list[dict] = field(default_factory=list)

    @property
    def levels(self) -> C.LevelCounts:
        return self.metrics.levels


# ============================================================ 主入口
def run_inference(
    df: pd.DataFrame,
    horizon: int = 7,
    model: torch.nn.Module | None = None,
    feat_names: tuple[str, ...] | None = None,
    window: int = C.WINDOW_SIZE,
    holiday: bool = True,
    device: str = "auto",
    target_mode: str = features.TARGET_MODE,
    baseline_window: int = C.BASELINE_WINDOW,
    backtest: bool = False,
    levels_for_main: tuple[str, ...] = ("L1",),
    debug: bool = False,
) -> InferenceResult:
    """完整在线流水线。df 应已通过 validate_dataframe 并按需 clean_dataframe。

    model=None 时全部走规则兜底 —— 这让算法链路可以在没有训练权重时先跑通，
    也方便单测（见 scripts/self_check.py）。

    backtest=True 时把每条序列的最后 horizon 天摘掉再预测，
    用于**离线验收**（scripts/eval_report.py）：只有这样指标才与
    train.evaluate_on_holdout 同口径，也才谈得上"样本外"。
    线上服务必须保持 backtest=False。

    levels_for_main 控制计入主指标的级别（默认只有 L1，理由见 metrics 模块说明）。
    """
    t_all = time.perf_counter()
    dev = preprocess.resolve_device(device)
    res = InferenceResult(device=dev)

    # ---------- 1. 校验（数据流 2：已验证过的可跳过，这里保留以防直调） ----------
    t0 = time.perf_counter()
    preprocess.validate_dataframe(df)
    work = preprocess.clean_dataframe(df)
    res.timing.preprocess_ms = int((time.perf_counter() - t0) * 1000)

    # ---------- 2. 分级（后续所有环节的分流依据） ----------
    infos = classify.classify_series(df)      # 用**原始**数据判有效性，不用清洗后
    res.coverage = classify.coverage_report(infos)
    res.metrics.levels = classify.count_levels(infos)

    if res.coverage["total"] == 0:
        res.warnings.append("输入中没有可识别的序列。")
        res.timing.total_ms = int((time.perf_counter() - t_all) * 1000)
        return res

    l3_count = res.coverage["L3"]
    if l3_count:
        res.warnings.append(
            f"{l3_count} 条序列历史不足，不输出数值预测（L3）。"
            f"这属于长尾数据集的正常情况。"
        )

    # ---------- 3. 特征 + 批量推理 ----------
    t0 = time.perf_counter()
    l1_keys = classify.l1_series(infos)
    windows: list[np.ndarray] = []
    window_meta: list[tuple[str, str, np.ndarray, np.ndarray, list[str]]] = []
    future_dates = _future_dates(work, horizon)

    for key in l1_keys:
        sku, wh = key
        sub = work[(work[C.COL_SKU] == sku) & (work[C.COL_WAREHOUSE] == wh)].sort_values(C.COL_DATE)
        vals = sub[C.COL_SALES].to_numpy(dtype=float)
        dates = sub[C.COL_DATE].reset_index(drop=True)

        # 🔒 backtest 模式：把最后 horizon 天从历史里摘掉，
        #    让"预测"变成真正的样本外预测（与离线评估口径一致）。
        #    线上正常运行时 backtest=False，用全部历史预测真正的未来。
        if backtest:
            vals = vals[:-horizon]
            dates = dates.iloc[:-horizon].reset_index(drop=True)

        if len(vals) < window:
            continue

        scaler = preprocess.Scaler.fit(vals)      # 逐序列归一化（在线无训练集概念，用全历史）
        norm = scaler.transform(vals)
        xw = features.build_inference_windows(dates, norm, window=window, holiday=holiday)
        if xw.shape[0] == 0:
            continue
        windows.append(xw[0])
        window_meta.append((sku, wh, scaler, norm, dates.dt.strftime("%Y-%m-%d").tolist()))

    res.timing.features_ms = int((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    # 构建返回记录
    for (sku, wh, scaler, norm, date_strs), _ in zip(window_meta, windows):
        res.records.append(PredictionRecord(
            sku=sku, warehouse=wh, level="L1",
            dates=[], forecast=None,
            note=infos[(sku, wh)].level_reason,
        ))

    if model is not None and windows:
        batch = torch.from_numpy(np.stack(windows, axis=0)).to(dev)
        model.eval()
        with torch.no_grad():
            # 一次性批量前向：这是 60s SLA 的关键
            preds = model(batch).cpu().numpy()
        res.device = dev
        res.timing.inference_ms = int((time.perf_counter() - t0) * 1000)

        all_dates = _union_future_dates(work, horizon)
        t_roll = time.perf_counter()
        for idx, (sku, wh, scaler, norm, date_strs) in enumerate(window_meta):
            # 滚动多步：把上一步的预测接到归一化序列尾部，重新构造窗口
            multi = _roll_multistep(model, scaler, norm, date_strs, horizon,
                                    window, holiday, dev, first_pred=float(preds[idx]),
                                    target_mode=target_mode, baseline_window=baseline_window)
            fc = [max(0.0, v) for v in multi]
            lo, hi = _confidence_band(fc, norm, infos[(sku, wh)].level)
            res.records[idx].forecast = fc
            res.records[idx].lower = lo
            res.records[idx].upper = hi
            res.records[idx].dates = all_dates[:len(fc)]
        # ⏱ EP-5 埋点：这段是真实耗时的大头（逐序列 × 逐步 × 两遍）
        res.timing.rolling_ms = int((time.perf_counter() - t_roll) * 1000)
    else:
        # 无模型：L1 也退化为移动平均（保证链路可跑通，且结果仍可解释）
        for rec in res.records:
            sub = work[(work[C.COL_SKU] == rec.sku) & (work[C.COL_WAREHOUSE] == rec.warehouse)]
            vals = sub.sort_values(C.COL_DATE)[C.COL_SALES].to_numpy(dtype=float)
            fc = metrics_mod.moving_average_forecast(vals, horizon)
            rec.forecast = [max(0.0, float(v)) for v in fc]
            rec.dates = _union_future_dates(work, horizon)[:horizon]
            lo, hi = _confidence_band(rec.forecast, vals, "L1")
            rec.lower, rec.upper = lo, hi
            rec.note = "未加载模型权重，使用移动平均兜底"
        res.warnings.append("未提供模型权重，全部分支退化为移动平均。")

    # L2 / L3 兜底
    all_dates = _union_future_dates(work, horizon)
    t_fb = time.perf_counter()
    for (sku, wh), info in infos.items():
        if info.level == "L1":
            continue
        if info.level == "L2":
            fc, note = fallback.l2_forecast(work, sku, wh, horizon)
            fc_list = [max(0.0, float(v)) if not np.isnan(v) else None for v in fc]
            if all(v is None for v in fc_list):
                fc_list = None
            res.records.append(PredictionRecord(
                sku=sku, warehouse=wh, level="L2",
                dates=all_dates[:horizon] if fc_list else [],
                forecast=fc_list, note=note["reason"],
            ))
        else:
            _none, note = fallback.l3_forecast(work, sku, wh)
            res.records.append(PredictionRecord(
                sku=sku, warehouse=wh, level="L3",
                dates=[], forecast=None,
                note=note["reason"],
            ))
    res.timing.fallback_ms = int((time.perf_counter() - t_fb) * 1000)

    # ---------- 4. 指标（含同点基线） ----------
    t0 = time.perf_counter()
    records = _to_metric_records(work, infos, res.records, horizon)
    res.metrics = metrics_mod.compute_task_metrics(
        records, horizon, levels_for_main=levels_for_main)
    res.metric_records = records
    res.distribution = metrics_mod.series_mape_distribution(records)
    res.timing.metrics_ms = int((time.perf_counter() - t0) * 1000)
    if not res.metrics.levels.total:
        res.metrics.levels = classify.count_levels(infos)

    res.timing.total_ms = int((time.perf_counter() - t_all) * 1000)

    # SLA 自检：超了就明确报警，不要静默变慢
    if res.timing.total_ms > C.SLA_SECONDS * 1000:
        res.warnings.append(
            f"⚠️ 本次耗时 {res.timing.total_ms / 1000:.1f}s，超过 {C.SLA_SECONDS}s 上限。"
            f"建议减少序列数或改用 CPU/GPU 另一侧。"
        )

    return res


# ============================================================ 辅助
def _future_dates(work: pd.DataFrame, horizon: int) -> list[str]:
    last = pd.to_datetime(work[C.COL_DATE]).max()
    return [(last + pd.Timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in range(horizon)]


def _union_future_dates(work: pd.DataFrame, horizon: int) -> list[str]:
    """全表统一未来窗口。所有序列的 date 列必须一致，否则结果表无法对齐。"""
    return _future_dates(work, horizon)


def _roll_multistep(
    model: torch.nn.Module,
    scaler: preprocess.Scaler,
    norm_history: np.ndarray,
    date_strs: list[str],
    horizon: int,
    window: int,
    holiday: bool,
    device: str,
    first_pred: float | None = None,
    target_mode: str = features.TARGET_MODE,
    baseline_window: int = C.BASELINE_WINDOW,
) -> list[float]:
    """递归多步预测。

    为什么是递归而不是直接多输出：
      1) 只需一个模型覆盖 7/14/30 三档步长，训练成本与维护成本都低；
      2) 多输出头需要为每个步长准备标签，稀疏序列能提供的样本更少。
    代价是误差会累积，所以结果页必须同时给出置信区间与 MAPE 披露。

    🔓 残差模式下的还原公式：预测值 = 移动平均基线 + 模型输出 × σ
       基线用**当前已有的历史**（含前面几步的预测值）重算 —— 与训练时
       features.baseline_values 的口径一致，不能换成固定值。

    ⚠️ 滚动时窗口里的日期必须是**真实日期**：日历特征（星期/月份/节假日）靠它算出来，
       早期版本用 "0"/"1" 之类占位符，走到第 110 个位置时 pd.to_datetime 直接抛错。
       现在由 `cal_full`（历史 + 未来 horizon 天）统一提供，不再每步重算。

    ⏱ 本函数是真实耗时的大头（逐序列 × 逐步），`EP-5` 已把每步的窗口构造
       从「整条序列走一遍 pandas」改为「纯 numpy 取尾部窗口」，
       详见函数体内注释与 `scripts/verify_ep5.py` 的等价性断言。
    """
    seq = list(np.asarray(norm_history, dtype=float))
    dates = _normalize_dates(list(date_strs))
    out: list[float] = []
    sigma = scaler.std

    if not dates:
        return []

    # 🔥 性能关键：日历特征**一次算好**，窗口构造走纯 numpy 快速路径。
    #
    # 原实现每步调用 `build_inference_windows`，而它内部是「先对整条序列建特征表、
    # 再取最后 window 行」—— 于是同一份 pandas 流水线
    # （`pd.to_datetime` + `dt.strftime` + `isin` + `shift` + 2×`rolling` + `concat`）
    # 被重复执行 `horizon` 次。⏱ 实测（113 序列 / MX450）：
    #   horizon=7 → 19.7 s · horizon=14 → 36.0 s · horizon=30 → **72.2 s（击穿 60 s）**
    # 而一次批量前向只要 150 ms —— 也就是说**滚动段的窗口构造**吃掉了 90% 的时间。
    #
    # 正确性论证（等价，而非近似）：
    #   1) 日期在滚动中是**确定增长**的（每天 +1），故日历特征可一次算好；
    #   2) 其余各列只依赖「它之前的至多 ROLL_LOOKBACK 个值」——
    #      lag_1 → 1，lag_7 → 7，roll_mean_7 → 8，roll_mean_14 → 15，
    #      由 `features.last_window_fast` 用同一组公式逐行复刻。
    #
    # 🔒 等价性不靠注释担保：`scripts/verify_ep5.py` 会在真实数据上，
    #    用改造前的 `build_inference_windows` 与现实现逐位比对全部 L1 序列的
    #    滚动结果（`np.array_equal`），并要求评测指标全等。
    last_str = dates[-1]
    future = [
        (pd.Timestamp(last_str) + pd.Timedelta(days=i + 1)).strftime("%Y-%m-%d")
        for i in range(horizon)
    ]
    cal_full = features.calendar_matrix(dates + future)

    for step in range(horizon):
        # `min(...)` 只在 `_normalize_dates` 丢弃过无法解析的日期时才起作用：
        # 那种情况下两者长度不再相等，取公共前缀与旧实现的逐行对齐语义一致。
        n = min(len(seq), len(cal_full))
        xw = features.last_window_fast(
            cal_full[:n], np.asarray(seq[:n], dtype=float), window=window, holiday=holiday)
        if xw.shape[0] == 0:
            break
        with torch.no_grad():
            v = float(model(torch.from_numpy(xw).to(device)).item())

        if target_mode == features.TARGET_RESIDUAL:
            # 用已还原到原量纲的历史算基线（含前几步的预测）
            raw_hist = scaler.inverse(np.asarray(seq, dtype=float))
            base_now = metrics_mod.moving_average_forecast(raw_hist, 1, baseline_window)[0]
            real = features.residual_to_value(v, base_now, sigma)
        else:
            real = float(scaler.inverse(np.array([v]))[0])

        real = max(0.0, real)
        out.append(real)

        # 🔒 关键：把**归一化后**的值接回序列继续滚动。
        #    直接接真实值会让模型的输入量纲与训练时不一致（训练输入是 /σ 的），
        #    这是最容易被忽略的一处错位 —— 不会报错，但预测会完全跑偏。
        seq.append(float(scaler.transform(np.array([real]))[0]))
        # 日期无需在此推进：`cal_full` 已把「历史 + 未来 horizon 天」的日历特征
        # 一次算好，第 step 步用的就是第 len(history)+step 行。

    if first_pred is not None and out:
        raw_hist = scaler.inverse(np.asarray(seq[:len(norm_history)], dtype=float))
        base_now = metrics_mod.moving_average_forecast(raw_hist, 1, baseline_window)[0]
        if target_mode == features.TARGET_RESIDUAL:
            out[0] = max(0.0, features.residual_to_value(first_pred, base_now, sigma))
        else:
            out[0] = max(0.0, float(scaler.inverse(np.array([first_pred]))[0]))
    return out


def _normalize_dates(raw: list) -> list[str]:
    """把日期统一成 YYYY-MM-DD 字符串。

    调用方可能传 Timestamp、date 或字符串；日历特征一律按字符串处理，
    避免 pandas 在混合类型上报 "time data '0' doesn't match format" 这类错误。
    """
    out: list[str] = []
    for x in raw:
        try:
            out.append(pd.Timestamp(x).strftime("%Y-%m-%d"))
        except Exception:
            # `R-12`：原来这里是裸 `continue`。一旦日历特征整列解析失败
            # （如上游把日期写成 epoch 数字），预测照常出结果但**没有任何痕迹**。
            # 用 debug 级：逐值兜底，正常数据里不该出现，排查时能开日志定位。
            logger.debug("日期归一化跳过不可解析的值：%r", x, exc_info=True)
            continue
    return out


def _confidence_band(forecast: list[float], history: np.ndarray,
                     level: str) -> tuple[list[float], list[float]]:
    """置信区间。

    ⚠️ 这是**经验区间**，不是统计意义上的预测区间：
       用历史波动（MAD）随步长开方放大，越靠后越宽。
       之所以不做严格区间估计，是因为 LSTM 的输出不附带分布，
       给出一个"看起来严谨但无依据"的区间反而误导用户。
       结果页必须标注「经验区间，仅供范围参考」（PRD FR-VIS-03）。
    """
    h = np.asarray(history, dtype=float)
    h = h[~np.isnan(h)]
    if h.size == 0 or not forecast:
        return [], []
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med))) * 1.4826
    sigma = mad if mad > 0 else float(np.std(h)) or 1.0
    # L2/L3 的不确定性更大
    mult = {"L1": 1.0, "L2": 1.6, "L3": 2.0}.get(level, 1.0)

    lower, upper = [], []
    for i, v in enumerate(forecast):
        width = 1.28 * sigma * mult * np.sqrt(i + 1)     # 1.28 ≈ 80% 分位
        lower.append(max(0.0, v - width))
        upper.append(v + width)
    return lower, upper


def _to_metric_records(
    work: pd.DataFrame,
    infos: dict,
    preds: list[PredictionRecord],
    horizon: int,
) -> list[dict]:
    """把预测记录转成指标模块需要的结构，并在**同一批点**上算基线。

    🔒 两个必须守住的公平性约束：

    1) **基线与模型用同一批预测点、同一序列集合**
       若基线用了更长的历史或更多的序列，比较就没有意义。

    2) **真值不得出现在预测所用的历史里**（本次踩坑修正）
       早期实现在这里取 vals[-horizon:] 当真值，但同一批 vals 又被用来构造
       预测窗口 —— 等于"看着答案做预测"，指标虚高且与离线评估不可比。
       正确做法：真值取最后 horizon 天，提供给模型的历史必须**截到
       len(vals) - horizon**。因此本函数只负责配对，
       真正的"截断历史"由 run_inference 的 backtest 参数控制。

    3) **L2/L3 不参与模型能力评估**
       L2 的误差由规则（商品级均值 × 占比）决定，不是 LSTM 的能力。
       把它们混进加权 MAPE 会把"模型是否达标"这个问题问歪 ——
       实测 L2 组 MAPE 高达 60%，足以把一个 L1 达标的模型整体拖成不达标。
       因此评估口径按 level 分组呈现，主指标只统计 L1。
    """
    records: list[dict] = []
    pred_map = {(p.sku, p.warehouse): p for p in preds}

    for (sku, wh), info in infos.items():
        p = pred_map.get((sku, wh))
        if p is None or info.level == "L3":
            records.append({"sku": sku, "warehouse": wh, "level": info.level,
                            "y_true": [], "y_pred": None, "baseline": None})
            continue

        sub = work[(work[C.COL_SKU] == sku) & (work[C.COL_WAREHOUSE] == wh)].sort_values(C.COL_DATE)
        vals = sub[C.COL_SALES].to_numpy(dtype=float)
        vals = vals[~np.isnan(vals)]
        if vals.size < horizon + 1:
            records.append({"sku": sku, "warehouse": wh, "level": info.level,
                            "y_true": [], "y_pred": None, "baseline": None})
            continue

        # 真值 = 最后 horizon 天；历史 = 之前的部分（与 run_inference 的截断一致）
        truth = vals[-horizon:]
        hist = vals[:-horizon]
        # 🔒 基线用与模型完全相同的历史窗口与预测步数
        base = metrics_mod.moving_average_forecast(hist, horizon)
        pred = p.forecast or []
        n = min(len(truth), len(pred))
        if n == 0:
            records.append({"sku": sku, "warehouse": wh, "level": info.level,
                            "y_true": [], "y_pred": None, "baseline": None})
            continue

        records.append({
            "sku": sku, "warehouse": wh, "level": info.level,
            "y_true": truth[-n:].tolist(),
            "y_pred": pred[:n],
            "baseline": base[-n:].tolist(),
        })

    return records


def save_bundle(
    model: torch.nn.Module,
    path: str,
    meta: dict | None = None,
) -> dict:
    """保存模型权重 + 元信息，供 infer 侧加载（写 model_registry 前落盘）。"""
    payload = {
        "state_dict": model.state_dict(),
        "n_features": getattr(model, "n_features", features.N_FEATURES),
        "hidden_size": getattr(model, "hidden_size", C.HIDDEN_SIZE),
        "num_layers": getattr(model, "num_layers", C.NUM_LAYERS),
        "feat_names": list(features.FEATURE_NAMES),
        "meta": meta or {},
    }
    torch.save(payload, path)
    return {k: v for k, v in payload.items() if k != "state_dict"}


def load_bundle(path: str, device: str = "cpu") -> tuple[torch.nn.Module, dict]:
    """加载权重。返回 (model, meta)。

    ⚠️ `EP-5` 修正：`save_bundle` 把 `feat_names` / `n_features` 放在 payload
       **顶层**，而本函数只回传 `meta` —— 于是「用哪几个特征、什么顺序」
       在加载侧完全丢失。训练时 `FEATURE_NAMES` 一旦调整，在线构造的输入
       张量列序就会与网络权重错位：**不报错、不崩溃，只是预测悄悄跑偏**，
       是本项目里最难发现的一类缺陷（同型：`_roll_multistep` 里
       「接回归一化值而不是真实值」）。
       故此处把顶层字段**合并进 meta**（不覆盖 meta 里已有的同名键），
       让调用方一次就能拿到全部元信息。
    """
    from . import model as model_mod
    payload = torch.load(path, map_location=device, weights_only=False)
    m = model_mod.build_model(
        payload.get("n_features", features.N_FEATURES),
        payload.get("hidden_size", C.HIDDEN_SIZE),
        payload.get("num_layers", C.NUM_LAYERS),
    )
    m.load_state_dict(payload["state_dict"])
    m.to(device)
    m.eval()
    meta = dict(payload.get("meta", {}))
    for key in ("feat_names", "n_features", "hidden_size", "num_layers"):
        if key in payload and key not in meta:
            meta[key] = payload[key]
    return m, meta
