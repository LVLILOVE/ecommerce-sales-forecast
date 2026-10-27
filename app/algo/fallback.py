"""L2 / L3 规则兜底。

📌 数据事实决定这是**正常路径**而不是异常分支：
    天池 167097 数据集 963 商品 × 5 仓 ≈ 4,815 条理论序列，
    其中 17,063 条仓库样本缺失，大量长尾商品只在 1–2 个仓有货。
    所以「大部分序列进不了 LSTM」是常态，兜底质量直接决定产品可用性。

    L2  商品级降级：预测商品整体销量，再按各仓历史占比分摊回本仓
    L3  不可预测：**不输出数值预测**（PRD FR-MOD-09 硬要求），
        只给出历史均值供人工参考，并在结果页显式标注"不可预测"
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .metrics import moving_average_forecast


def item_aggregate(df: pd.DataFrame, sku: str) -> pd.Series:
    """把同一商品的所有仓按日期汇总成一条商品级序列（L2 的输入）。"""
    sub = df[df[C.COL_SKU] == sku]
    if sub.empty:
        return pd.Series(dtype=float)
    g = sub.groupby(C.COL_DATE, observed=True)[C.COL_SALES].sum()
    return g.sort_index()


def warehouse_shares(df: pd.DataFrame, sku: str) -> dict[str, float]:
    """各仓历史销量占比（L2 分摊依据）。

    用**占比**而不是绝对量，是因为占比在商品级预测值变化时仍然成立，
    不需要为每个仓单独建模 —— 这正是 L2 能做到 O(1) 成本的原因。
    """
    sub = df[df[C.COL_SKU] == sku]
    if sub.empty:
        return {}
    total = sub.groupby(C.COL_WAREHOUSE, observed=True)[C.COL_SALES].sum()
    denom = float(total.sum())
    if denom <= 0:
        n = max(len(total), 1)
        return {str(k): 1.0 / n for k in total.index}
    return {str(k): float(v) / denom for k, v in total.items()}


def l2_forecast(
    df: pd.DataFrame,
    sku: str,
    warehouse: str,
    horizon: int,
    window: int = C.BASELINE_WINDOW,
) -> tuple[np.ndarray, dict]:
    """L2 兜底：商品级移动平均外推 × 本仓历史占比。

    返回 (预测数组, 说明信息)。预测值不含负数（销量不能为负）。
    """
    agg = item_aggregate(df, sku)
    if agg.empty:
        return np.full(horizon, np.nan), {"strategy": "none", "reason": "无商品级历史"}

    item_fc = moving_average_forecast(agg.to_numpy(), horizon, window)
    shares = warehouse_shares(df, sku)
    share = shares.get(str(warehouse), 0.0)

    pred = np.clip(item_fc * share, 0.0, None)
    return pred, {
        "strategy": "item_level_allocate",
        "reason": (f"本仓历史不足以单独建模，改用商品级移动平均"
                   f"（近 {window} 天均值 {float(np.mean(agg.to_numpy()[-window:])):.1f}）"
                   f"× 本仓历史占比 {share:.1%}"),
        "item_mean": float(np.mean(agg.to_numpy()[-window:])),
        "share": share,
        "granularity_level": "L2",
    }


def l3_forecast(
    df: pd.DataFrame,
    sku: str,
    warehouse: str,
    *,
    series_values: np.ndarray | None = None,
    item_values: np.ndarray | None = None,
    global_values: np.ndarray | None = None,
) -> tuple[None, dict]:
    """L3 兜底：**不输出数值预测**，只给历史均值。

    🔒 这里必须返回 None，不能"给个大概数让它看起来完整"。
    PRD FR-MOD-09 / FR-VIS-04 明确要求：不可预测就显式说不可预测，
    否则用户会拿一个纯猜测的数字去补货，比没有数字更危险。

    历史均值的取值优先级（保证用户总能拿到一个参考值）：
        该仓自身历史 → 该商品跨仓历史 → 全局历史
    因为 L3 的典型场景就是"这个仓根本没有数据"，
    此时连它自己都没有均值，退到商品级才能给出有意义的参考。

    🔴 **三个 `*_values` 参数是性能逃生口**（2026-10-26 复评修复）：
    默认路径会对 `df` 做**两次全表布尔筛选**（序列级 + 商品级），
    1000 条序列 × 50 万行 ⇒ 实测约 74 秒，而且它发生在预算检查点之后
    ⇒ 60 秒端到端承诺被这一处单独架空。
    调用方（`predict_service._build_prediction_rows`）用 `groupby`
    一次建好索引后，把三个数组传进来即可 —— **口径与原实现逐字等价**。
    """
    if series_values is not None:
        vals = series_values
    else:
        sub = df[(df[C.COL_SKU] == sku) & (df[C.COL_WAREHOUSE] == warehouse)]
        vals = sub[C.COL_SALES].to_numpy(dtype=float) if not sub.empty else np.array([])
        vals = vals[~np.isnan(vals)]

    source = "warehouse"
    if vals.size == 0:
        # 本仓无历史 → 退到该商品跨仓历史（L3 最常见的情形）
        if item_values is not None:
            item_vals = item_values
        else:
            item_vals = df[df[C.COL_SKU] == sku][C.COL_SALES].to_numpy(dtype=float)
            item_vals = item_vals[~np.isnan(item_vals)]
        if item_vals.size:
            vals = item_vals
            source = "item"
    if vals.size == 0:
        # 该商品完全无历史 → 退到全局（仅作极粗参考）
        if global_values is not None:
            g = global_values
        else:
            g = df[C.COL_SALES].to_numpy(dtype=float)
            g = g[~np.isnan(g)]
        if g.size:
            vals = g
            source = "global"

    hist_mean = float(np.mean(vals)) if vals.size else float("nan")
    return None, {
        "strategy": "no_forecast",
        "reason": (f"本仓历史不足 {C.L1_MIN_VALID_POINTS} 天、跨仓聚合也不足，"
                   f"不输出数值预测"),
        "historical_mean": hist_mean,
        "historical_days": int(vals.size),
        "historical_source": source,
        "granularity_level": "L3",
        "predictable": False,
    }


def global_mean_fallback(df: pd.DataFrame, warehouse: str = "") -> float:
    """全局均值 —— 仅在连商品级历史都没有时的最后兜底（报告用，不作预测输出）。"""
    sub = df if not warehouse else df[df[C.COL_WAREHOUSE] == warehouse]
    if sub.empty:
        sub = df
    vals = sub[C.COL_SALES].to_numpy(dtype=float)
    vals = vals[~np.isnan(vals)]
    return float(np.mean(vals)) if vals.size else float("nan")


def coverage_note(report: dict) -> str:
    """覆盖度的人话解释，直接渲染到结果页（PRD FR-VIS-07「稀疏是常态，不是异常」）。"""
    total = report.get("total", 0)
    if not total:
        return "本次输入没有可评估序列。"
    l1 = report.get("L1", 0)
    l2 = report.get("L2", 0)
    l3 = report.get("L3", 0)
    return (
        f"本次共 {total} 条序列：L1 精确预测 {l1} 条、"
        f"L2 商品级降级 {l2} 条、L3 不可预测 {l3} 条。"
        f"长尾数据集中 L2/L3 属于正常现象，不是数据错误；"
        f"评估结果仅基于参与数值预测的 {l1 + l2} 条序列。"
    )
