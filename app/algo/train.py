"""训练：滑动窗口样本构建 → LSTM → 早停 → 学习率衰减 → 网格搜索。

📌 **仅离线调用**（架构红线 2）：`services/` 不得 import 本模块。
   服务进程内存里不应存在训练相关依赖与图构建逻辑。

⏱ 本机实测：hidden=32/layers=1/window=14，100k 样本 CPU 3.67s / GPU 0.84s，
   峰值显存 111MB。所以训练本身很快，1 分钟 SLA 排除在线训练的**真正原因**是
   「训练 + 网格搜索 + 早停无法稳定收敛在 60 秒内」，而不是算力不够。
   因此线上固定为「预训练权重 + 批量推理」。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from . import config as C
from . import features, model as model_mod, preprocess
from . import metrics as metrics_mod

logger = logging.getLogger(__name__)


@dataclass
class TrainConfig:
    """训练超参。默认值即 PRD 推荐值，改这里等于改线上模型行为。"""

    hidden_size: int = C.HIDDEN_SIZE
    num_layers: int = C.NUM_LAYERS
    window: int = C.WINDOW_SIZE
    dropout: float = C.DROPOUT
    batch_size: int = C.BATCH_SIZE
    lr: float = C.LR
    max_epochs: int = C.MAX_EPOCHS
    patience: int = C.PATIENCE
    lr_decay_gamma: float = C.LR_DECAY_GAMMA
    lr_decay_step: int = C.LR_DECAY_STEP
    grad_clip: float = C.GRAD_CLIP
    device: str = "auto"
    seed: int = 42
    val_ratio: float = 0.1          # 从训练段尾部再切一段做早停验证（仍是时序切分）

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class TrainResult:
    model: nn.Module | None
    config: TrainConfig
    best_val_loss: float
    best_epoch: int
    epochs_run: int
    seconds: float
    n_train: int
    n_val: int
    n_features: int
    history: list[dict] = field(default_factory=list)
    device: str = "cpu"

    def summary(self) -> dict:
        return {
            "hidden_size": self.config.hidden_size,
            "num_layers": self.config.num_layers,
            "window": self.config.window,
            "n_features": self.n_features,
            "n_params": model_mod.count_params(self.model) if self.model else 0,
            "n_train": self.n_train,
            "n_val": self.n_val,
            "best_epoch": self.best_epoch,
            "epochs_run": self.epochs_run,
            "best_val_loss": round(self.best_val_loss, 6),
            "seconds": round(self.seconds, 2),
            "device": self.device,
        }


# ============================================================ 样本构建
@dataclass
class TrainSet:
    """训练样本集。

    X: (n_samples, window, n_features)   —— 特征列已逐序列归一化
    y: (n_samples, 1)                    —— 残差模式下 std ≈ 1（因为已 /σ）
    seq_id: (n_samples,)                 —— 样本所属序列编号
        为什么必须带序列编号：不同商品的销量量级差两个数量级，
        把它们的样本**交错打乱**会让模型在多个分布间来回跳，学不到稳定规律
        （实测：val_loss 停在 10.0 不再下降，即残差 std 只有 ~1/3）。
        正确做法是按序列切分训练/验证，见 split_by_series()。
    """

    X: np.ndarray
    y: np.ndarray
    seq_id: np.ndarray
    feat_names: tuple[str, ...]
    series_keys: list[tuple[str, str]]

    @property
    def n_samples(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_series(self) -> int:
        return len(self.series_keys)


def build_training_set(
    df: pd.DataFrame,
    horizon: int = 1,
    window: int = C.WINDOW_SIZE,
    holiday: bool = True,
    level_infos: dict | None = None,
    target_mode: str = features.TARGET_MODE,
    baseline_window: int = C.BASELINE_WINDOW,
) -> tuple[TrainSet, dict]:
    """把 DataFrame 转成监督学习样本。

    🔑 两个关键设计（都来自实测踩坑）：

    1) **逐序列独立归一化 + 学残差**
       统计量从该序列的**训练段**拟合，然后 transform 整段（含验证段）。
       若用整段统计量，验证集分布会泄漏进训练（风险 R2），指标虚高、上线即崩。
       目标再改为「相对移动平均基线的残差 / σ」（见 features.TARGET_MODE）：
       直接回归绝对值会让预测压平到真值波动的 26%，MAE 只比基线好 11.7%。

    2) **特征列也归一化，并按序列携带编号**
       原始销量量级跨两个数量级；同时把不同序列的样本交错混在一起，
       模型会在多个分布间来回跳 —— 实测 val_loss 卡在 10.0（残差 std ≈ 3.2）。
       归一化后残差 std ≈ 1，val_loss 才落到 1 附近并持续下降。

    只使用 L1 序列训练（L2/L3 走规则兜底，不参与训练）。
    """
    if level_infos is None:
        from . import classify as classify_mod
        level_infos = classify_mod.classify_series(df)
    targets = [k for k, v in level_infos.items() if v.level == "L1"]

    Xs, ys, ids = [], [], []
    skipped_short = 0
    skipped_level = 0
    skipped_degenerate = 0
    sigmas: list[float] = []
    series_keys: list[tuple[str, str]] = []
    per_series_counts: dict[str, int] = {}

    for (sku, wh) in targets:
        sub = df[(df[C.COL_SKU] == sku) & (df[C.COL_WAREHOUSE] == wh)]
        sub = sub.sort_values(C.COL_DATE)
        if len(sub) < features.min_required_points(window):
            skipped_short += 1
            continue

        dates = sub[C.COL_DATE].reset_index(drop=True)
        vals = sub[C.COL_SALES].to_numpy(dtype=float)
        if np.sum(~np.isnan(vals)) < C.L1_MIN_VALID_POINTS:
            skipped_level += 1
            continue

        # 训练段 = 前 80%（严格按时间）
        tr, _va, _te = preprocess.chronological_split(len(vals))
        train_vals = vals[tr]
        scaler = preprocess.Scaler.fit(train_vals)      # 🔒 只用训练段拟合

        # 🔒 退化序列剔除：σ 极小意味着序列几乎恒定（实测有全零序列 σ=0.03）。
        #    这类序列的残差被除以一个极小的数，放大成 10~15 倍的伪信号，
        #    在池化损失里权重压倒性地大，把模型整体带偏。
        #    它们的预测本来就不需要模型 —— 移动平均已是最优解。
        if scaler.std < C.MIN_USABLE_SIGMA:
            skipped_degenerate += 1
            continue

        norm = scaler.transform(vals)
        sigmas.append(scaler.std)

        ws = features.build_windows(
            dates, norm, window=window, horizon=horizon, holiday=holiday,
            sigma=scaler.std, target_mode=target_mode, baseline_window=baseline_window,
        )
        if ws.X.shape[0] == 0:
            skipped_short += 1
            continue

        sid = len(series_keys)
        series_keys.append((sku, wh))
        Xs.append(ws.X)
        ys.append(ws.y.reshape(-1, 1))
        ids.append(np.full(ws.X.shape[0], sid, dtype=int))
        per_series_counts[f"{sku}|{wh}"] = int(ws.X.shape[0])

    empty = TrainSet(
        X=np.zeros((0, window, features.N_FEATURES), dtype=np.float32),
        y=np.zeros((0, 1), dtype=np.float32), seq_id=np.zeros((0,), dtype=int),
        feat_names=features.FEATURE_NAMES, series_keys=[],
    )
    info_base = {
        "series_used": 0, "skipped_short": skipped_short,
        "skipped_level": skipped_level, "per_series": {},
    }
    if not Xs:
        return empty, info_base

    X = np.concatenate(Xs, axis=0)
    y = np.concatenate(ys, axis=0)
    seq_id = np.concatenate(ids, axis=0)
    info = {
        "series_used": len(Xs),
        "skipped_short": skipped_short,
        "skipped_level": skipped_level,
        "skipped_degenerate": skipped_degenerate,
        "per_series": per_series_counts,
        "n_features": X.shape[2],
        "target_mode": target_mode,
        "baseline_window": baseline_window,
        "sigma_median": float(np.median(sigmas)) if sigmas else 1.0,
        "y_std": float(np.std(y)),
    }
    return TrainSet(X=X, y=y, seq_id=seq_id, feat_names=features.FEATURE_NAMES,
                    series_keys=series_keys), info


def split_by_series(
    ts: TrainSet,
    val_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[TrainSet, TrainSet]:
    """**按序列**切分训练/验证集（不是按样本切）。

    为什么必须按序列切：
      同一序列相邻日期的样本几乎相同，按样本切会让"昨天的样本在训练集、
      今天的在验证集"，验证损失随之虚低 —— 看起来收敛得很好，其实是泄漏。
      按序列切分后验证集包含的是**完全没见过的商品/仓组合**，
      这才是对"上线后遇到新序列"的真实模拟。

    代价：验证集偏小、方差偏大（序列数少时尤其明显）。
      因此早停只据此判断趋势，不追求验证损失的绝对值最低。
    """
    rng = np.random.default_rng(seed)
    n_series = ts.n_series
    if n_series < 4:
        # 序列太少，退化为按时间尾部切（仍然不跨序列混样）
        n = ts.n_samples
        cut = max(1, int(n * (1 - val_ratio)))
        return (_subset(ts, slice(0, cut)), _subset(ts, slice(cut, n)))

    idx = rng.permutation(n_series)
    n_val = max(1, int(round(n_series * val_ratio)))
    val_series = set(idx[:n_val].tolist())

    val_mask = np.isin(ts.seq_id, list(val_series))
    tr_mask = ~val_mask
    return (
        TrainSet(ts.X[tr_mask], ts.y[tr_mask], ts.seq_id[tr_mask],
                 ts.feat_names, [k for i, k in enumerate(ts.series_keys) if i not in val_series]),
        TrainSet(ts.X[val_mask], ts.y[val_mask], ts.seq_id[val_mask],
                 ts.feat_names, [k for i, k in enumerate(ts.series_keys) if i in val_series]),
    )


def _subset(ts: TrainSet, sl: slice) -> TrainSet:
    return TrainSet(ts.X[sl], ts.y[sl], ts.seq_id[sl], ts.feat_names, ts.series_keys)


# ============================================================ 单次训练
def train_one(
    ts: TrainSet,
    cfg: TrainConfig,
    verbose: bool = True,
) -> TrainResult:
    """训练一个模型，按验证损失早停。

    早停为什么必要：LSTM 在长尾小样本上极易过拟合，且训练损失仍在下探时
    验证损失已经掉头 —— 不早停会拿到一个"训练集看起来很好"的废模型。
    验证集按**序列**切分（split_by_series），不是按样本。
    """
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    device = preprocess.resolve_device(cfg.device)
    n, win, n_feat = ts.X.shape
    if n == 0:
        raise ValueError("训练样本为 0，无法训练")

    tr_set, va_set = split_by_series(ts, val_ratio=cfg.val_ratio, seed=cfg.seed)
    Xtr, ytr = tr_set.X, tr_set.y
    Xva, yva = va_set.X, va_set.y
    if Xva.shape[0] == 0:
        # 序列数过少导致验证集为空：退化为取训练集尾部一小段
        cut = max(1, int(Xtr.shape[0] * 0.9))
        Xtr, Xva = Xtr[:cut], Xtr[cut:]
        ytr, yva = ytr[:cut], ytr[cut:]

    model = model_mod.build_model(n_feat, cfg.hidden_size, cfg.num_layers, cfg.dropout).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr)
    # 学习率衰减：验证损失停滞时减半，帮助跳出平台期
    sched = torch.optim.lr_scheduler.StepLR(opt, step_size=cfg.lr_decay_step,
                                            gamma=cfg.lr_decay_gamma)
    lossf = nn.MSELoss()          # 📌 PRD 指定 MSE

    tr_ds = TensorDataset(torch.from_numpy(Xtr), torch.from_numpy(ytr))
    tr_dl = DataLoader(tr_ds, batch_size=min(cfg.batch_size, max(1, len(tr_ds))), shuffle=True)

    xva = torch.from_numpy(Xva).to(device)
    yva = torch.from_numpy(yva).to(device)

    best_loss = float("inf")
    best_state = None
    best_epoch = 0
    wait = 0
    history: list[dict] = []
    t0 = time.perf_counter()

    for epoch in range(1, cfg.max_epochs + 1):
        model.train()
        running = 0.0
        nb = 0
        for xb, yb in tr_dl:
            xb = xb.to(device)
            yb = yb.to(device)
            opt.zero_grad()
            pred = model(xb).unsqueeze(-1)
            loss = lossf(pred, yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)   # 防梯度爆炸
            opt.step()
            running += float(loss.item())
            nb += 1
        sched.step()
        train_loss = running / max(nb, 1)

        model.eval()
        with torch.no_grad():
            val_pred = model(xva).unsqueeze(-1)
            val_loss = float(lossf(val_pred, yva).item())

        history.append({
            "epoch": epoch,
            "train_loss": round(train_loss, 6),
            "val_loss": round(val_loss, 6),
            "lr": round(opt.param_groups[0]["lr"], 8),
        })

        if val_loss < best_loss - 1e-6:
            best_loss = val_loss
            best_epoch = epoch
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            wait = 0
        else:
            wait += 1
            if wait >= cfg.patience:
                if verbose:
                    logger.info(
                        "    早停于 epoch %s（最佳 epoch %s，验证损失 %.6f）",
                        epoch, best_epoch, best_loss,
                    )
                break

        if verbose and epoch % 10 == 0:
            logger.info(
                "    epoch %3d  train %.6f  val %.6f", epoch, train_loss, val_loss
            )

    seconds = time.perf_counter() - t0
    if best_state is not None:
        model.load_state_dict(best_state)
    model.eval()

    return TrainResult(
        model=model, config=cfg, best_val_loss=best_loss, best_epoch=best_epoch,
        epochs_run=len(history), seconds=seconds, n_train=len(Xtr), n_val=len(Xva),
        n_features=n_feat, history=history, device=device,
    )


# ============================================================ 网格搜索
def grid_search(
    ts: TrainSet,
    base: TrainConfig | None = None,
    grid: dict | None = None,
    verbose: bool = True,
) -> list[TrainResult]:
    """离线调参。⏱ 单组约 5–90 秒（本机 CPU 实测），全跑视组合数而定。

    只在离线执行：在线路径固定使用 model_registry 里已注册的那一组超参。

    ⚠️ window 属于**样本结构**参数，改了必须重建 X。
       所以本函数只搜模型结构参数（hidden / layers）；
       要搜 window，请用 scripts/train_and_register.py --grid，它会按 window 重新采样。
    """
    base = base or TrainConfig()
    grid = grid or {
        "hidden_size": C.GRID_HIDDEN,
        "num_layers": C.GRID_LAYERS,
    }

    combos = [{}]
    for key, values in grid.items():
        combos = [dict(c, **{key: v}) for c in combos for v in values]

    results: list[TrainResult] = []
    for i, combo in enumerate(combos, 1):
        cfg = TrainConfig(**{**base.as_dict(), **combo})
        if verbose:
            logger.info("  [%s/%s] %s", i, len(combos), combo)
        res = train_one(ts, cfg, verbose=False)
        results.append(res)
        if verbose:
            logger.info(
                "        val_loss %.6f  epochs %s  %.2fs",
                res.best_val_loss, res.epochs_run, res.seconds,
            )

    results.sort(key=lambda r: r.best_val_loss)
    return results


# ============================================================ 序列评估
def evaluate_on_holdout(
    model: nn.Module,
    df: pd.DataFrame,
    level_infos: dict,
    horizon: int = 1,
    window: int = C.WINDOW_SIZE,
    holiday: bool = True,
    device: str = "cpu",
    target_mode: str = features.TARGET_MODE,
    baseline_window: int = C.BASELINE_WINDOW,
) -> list[dict]:
    """在**测试段**（每序列最后 10%）上评估，产出 metrics.compute_task_metrics 的输入。

    这里同时算出基线，且基线用的是**完全相同的预测点** —— 公平性硬约束。
    """
    records: list[dict] = []
    model.eval()

    for (sku, wh), info in level_infos.items():
        sub = df[(df[C.COL_SKU] == sku) & (df[C.COL_WAREHOUSE] == wh)].sort_values(C.COL_DATE)
        if len(sub) < window + horizon + 15:
            records.append({
                "sku": sku, "warehouse": wh, "level": info.level,
                "y_true": [], "y_pred": None, "baseline": None,
            })
            continue

        dates = sub[C.COL_DATE].reset_index(drop=True)
        vals = sub[C.COL_SALES].to_numpy(dtype=float)

        if info.level != "L1":
            # L2/L3 走兜底，不评估 LSTM（它们的误差由规则决定，不属于模型能力）
            records.append({
                "sku": sku, "warehouse": wh, "level": info.level,
                "y_true": [], "y_pred": None, "baseline": None,
            })
            continue

        tr, va, te = preprocess.chronological_split(len(vals))
        scaler = preprocess.Scaler.fit(vals[tr])
        sigma = scaler.std

        # 用训练+验证段作为"历史"，预测测试段的前 horizon 天
        hist_end = va.stop
        history = vals[:hist_end]
        truth = vals[hist_end: hist_end + horizon]
        if len(truth) < horizon or np.isnan(truth).any():
            truth = truth[~np.isnan(truth)]
            if len(truth) == 0:
                continue

        norm_hist = scaler.transform(history)
        d_hist = dates.iloc[:hist_end].reset_index(drop=True)

        # 滚动多步推理（与 infer.py 同源逻辑）
        preds: list[float] = []
        working = list(norm_hist)
        working_dates = list(d_hist)
        for step in range(len(truth)):
            xw = features.build_inference_windows(
                pd.Series(working_dates), np.asarray(working), window=window, holiday=holiday)
            if xw.shape[0] == 0:
                break
            with torch.no_grad():
                out = model(torch.from_numpy(xw).to(device)).item()

            # 🔓 还原：残差模式下要先算基准点（此刻的移动平均），再加回模型残差
            base_now = metrics_mod.moving_average_forecast(
                np.asarray(working) * sigma + scaler.mean, 1, baseline_window)[0]
            if target_mode == features.TARGET_RESIDUAL:
                v = features.residual_to_value(out, base_now, sigma)
            else:
                v = float(scaler.inverse(np.array([out]))[0])
            v = max(0.0, v)
            preds.append(v)
            # 接回**归一化空间**继续滚动（模型只看归一化输入）
            working.append(float(scaler.transform(np.array([v]))[0]))
            working_dates.append(dates.iloc[hist_end + step])

        n_eval = min(len(preds), len(truth))
        if n_eval == 0:
            continue

        # 🔒 基线：同一批预测点，用历史的移动平均
        base = metrics_mod.moving_average_forecast(history, n_eval)

        records.append({
            "sku": sku, "warehouse": wh, "level": info.level,
            "y_true": truth[:n_eval].tolist(),
            "y_pred": preds[:n_eval],
            "baseline": base.tolist(),
        })

    return records
