"""7 步校验的**编排**（`inspect_file()`，`R-06` 拆解后的主体）。

这个函数的职责被刻意压到只剩「按顺序调用 + 短路返回」：
**任何判定逻辑都不写在这里**。加第 8 步校验时，正确做法是往
`steps.py` 加一个 `check_xxx()`，然后在本文件的调用序列里插一行 ——
而不是继续往这个函数里堆 `if`。

🔒 调用顺序必须与 `algo.config.VALIDATE_ORDER` 一致（`FR-UPL-09`
   「校验顺序不可调整」，任一步失败即终止、不继续后续校验）。
"""
from __future__ import annotations

from pathlib import Path

from app.algo import config as algo_config
from app.validators import steps
from app.validators.csv_io import detect_encoding, load_csv
from app.validators.models import Inspection
from app.validators.stats import base_stats, build_preview, detect_gap_ranges

__all__ = ["inspect_file"]


def inspect_file(
    path: Path, *, size_bytes: int, original_rows: int | None = None
) -> Inspection:
    """按 `VALIDATE_ORDER` 巡检已落盘的文件。

    前置条件：扩展名（第 1 步）与文件大小（第 2 步的字节部分）已在路由层通过，
    故本函数从「行数」继续。返回值中的 `issue` 非空即表示失败。
    """
    # ---------------- 第 2 步 · 大小 / 行数 ----------------
    issue = steps.check_size(size_bytes=size_bytes, original_rows=original_rows)
    if issue is not None:
        return issue

    encoding = detect_encoding(path)

    # ---------------- 第 2 步（尾）+ 第 3 步 · 表头与结构 ----------------
    header, issue = steps.check_structure(path, encoding)
    if issue is not None:
        return issue

    df, _enc = load_csv(path, encoding)
    # 防御性复核：CSV 含引号换行时按行计数会偏小，这里以解析结果兜底
    issue = steps.check_row_limit(len(df))
    if issue is not None:
        return issue

    columns = [str(c) for c in df.columns]
    # CSV 行号 = DataFrame 行下标 + 2（第 1 行是表头）
    row_no = (df.index + 2).to_numpy()

    # ---------------- 第 4 步 · 日期可解析性 ----------------
    parsed_date, issue = steps.check_dates(df, columns, row_no)
    if issue is not None:
        return issue

    # ---------------- 第 5 步 · 销量非负 ----------------
    sales, issue = steps.check_sales(df, columns, row_no)
    if issue is not None:
        return issue

    df = df.assign(**{"_d": parsed_date, "_s": sales})

    # ---------------- 第 6 步 · 时间连续性（断档 → 待用户确认） ----------------
    gap_ranges, gap_count, gap_days_total = detect_gap_ranges(df)

    # ---------------- 第 7 步 · 序列数上限 ----------------
    series_count = int(
        df.groupby(
            [algo_config.COL_SKU, algo_config.COL_WAREHOUSE], observed=True
        ).ngroups
    )
    issue = steps.check_series_limit(
        series_count=series_count, gap_count=gap_count, df=df, columns=columns
    )
    if issue is not None:
        return issue

    return Inspection(
        stats=base_stats(df, series_count),
        preview=build_preview(df),
        gap_ranges=gap_ranges,
        gap_count=gap_count,
        gap_days_total=gap_days_total,
        error_columns=columns,
    )
