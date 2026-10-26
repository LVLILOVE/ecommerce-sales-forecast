"""统计与派生视图（从 `dataset_service` 拆出，`R-06`）。

与 `csv_io` 同样是「不只属于某一步校验」的公共件：断档检测服务于第 6 步，
但结果也要写进响应与 `datasets` 行；统计摘要与预览则要在校验**通过后**才算。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from app.algo import config as algo_config
from app.validators.constants import MAX_GAP_RANGES_RETURNED, PREVIEW_ROWS
from app.validators.csv_io import parse_dates, to_number

__all__ = ["base_stats", "detect_gap_ranges", "build_preview"]

#: 断档区间的「补一天 / 退一天」偏移。
#:
#: 🔴 **必须显式给 `unit="D"`**。pandas 2.3 + numpy 2.5 下 `pd.Timedelta(days=1)`
#:    与 `pd.Timedelta("1D")` 都会触发
#:    `DeprecationWarning: The 'generic' unit for NumPy timedelta is deprecated`，
#:    而 `pyproject.toml` 里 `filterwarnings = ["error::DeprecationWarning:app.*"]`
#:    会把它**升级成异常** ⇒ 只要数据里有断档，上传接口就 500。
#:    这个缺陷是 `R-06` 拆包后补单测时暴露的：旧代码一直是同样的写法，
#:    但 `verify_*` 脚本不跑 pytest 的告警过滤，所以从来没红过。
_ONE_DAY = pd.Timedelta(1, unit="D")


def base_stats(df: pd.DataFrame, series_count: int) -> dict[str, Any]:
    """统计摘要（写入 `datasets` 的 4 个计数字段 + 起止日期）。"""
    dates = df["_d"] if "_d" in df.columns else parse_dates(df[algo_config.COL_DATE])
    valid = dates.dropna()
    return {
        "row_count": int(len(df)),
        "sku_count": int(df[algo_config.COL_SKU].nunique()),
        "warehouse_count": int(df[algo_config.COL_WAREHOUSE].nunique()),
        "valid_series_count": int(series_count),
        "date_start": valid.min().strftime("%Y-%m-%d") if len(valid) else None,
        "date_end": valid.max().strftime("%Y-%m-%d") if len(valid) else None,
    }


def detect_gap_ranges(df: pd.DataFrame) -> tuple[list[dict[str, Any]], int, int]:
    """向量化检测每条序列内部的日期断档（`FR-UPL-12`）。

    返回 `(断档区间列表, 断档区间总数, 缺失天数合计)`。

    ⚠️ 只检测序列**内部**的空洞，不做两端外推 —— 与 `algo.preprocess.interpolate_series`
       的口径一致（序列首尾之前/之后「没有数据」不是断档，是还没开始/已经结束）。
    """
    if df.empty:
        return [], 0, 0
    frame = df.sort_values(
        [algo_config.COL_SKU, algo_config.COL_WAREHOUSE, "_d"], kind="mergesort"
    )
    grouped = frame.groupby(
        [algo_config.COL_SKU, algo_config.COL_WAREHOUSE], observed=True, sort=False
    )
    prev = grouped["_d"].shift(1)
    delta = (frame["_d"] - prev).dt.days
    mask = delta > 1
    if not bool(mask.any()):
        return [], 0, 0

    hit = frame.loc[mask, [algo_config.COL_SKU, algo_config.COL_WAREHOUSE, "_d"]].copy()
    hit["_prev"] = prev[mask]
    hit["_days"] = (delta[mask] - 1).astype(int)
    total_days = int(hit["_days"].sum())
    hit = hit.sort_values(
        [algo_config.COL_SKU, algo_config.COL_WAREHOUSE, "_prev"], kind="mergesort"
    )

    ranges: list[dict[str, Any]] = []
    for _, r in hit.head(MAX_GAP_RANGES_RETURNED).iterrows():
        ranges.append(
            {
                "sku_id": str(r[algo_config.COL_SKU]),
                "warehouse_id": str(r[algo_config.COL_WAREHOUSE]),
                "gap_start": (r["_prev"] + _ONE_DAY).strftime("%Y-%m-%d"),
                "gap_end": (r["_d"] - _ONE_DAY).strftime("%Y-%m-%d"),
                "gap_days": int(r["_days"]),
            }
        )
    return ranges, int(len(hit)), total_days


def build_preview(df: pd.DataFrame, rows: int = PREVIEW_ROWS) -> dict[str, Any]:
    """预览载荷（`FR-UPL-07`：表头与数据类型与源文件一致）。

    只对 `sales` 做数值化（整数优先），其余列保留原始字符串字面值。

    ⚠️ 必须剔除内部辅助列（`_d` 解析后的日期、`_s` 数值化后的销量）：
       它们是校验过程的中间产物，泄漏到预览会让「表头与源文件一致」这条
       `FR-UPL-07` 验收要点直接失败（EP-4 实测踩到）。
    """
    columns = [str(c) for c in df.columns if not str(c).startswith("_")]
    out: list[dict[str, Any]] = []
    for _, r in df.head(rows).iterrows():
        item = {c: str(r[c]) for c in columns}
        if algo_config.COL_SALES in item:
            item[algo_config.COL_SALES] = to_number(r[algo_config.COL_SALES])
        out.append(item)
    return {"columns": columns, "rows": out, "total": int(len(df)), "shown": len(out)}
