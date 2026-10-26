"""7 步校验中的「单步」函数（`R-06`：把 273 行的 `inspect_file()` 拆成 6 步）。

拆解原则
-----------------------------------------------------------------------------
**一步 = 一个函数 = 一个错误码家族**，函数只负责「判定 + 造 `Issue`」，
不负责编排（编排在 `pipeline.inspect_file()`）。这样第 8 步校验要加进来时，
只需要新增一个 `check_xxx()` 并在 pipeline 里插一行，不用再动 273 行的巨函数。

⚠️ 每个函数的返回约定统一为「失败返回 `Inspection`、通过返回 `None`」；
   需要往下游产出中间结果的（表头、日期列、销量列）返回
   `(值, 失败 Inspection | None)` 二元组 —— 刻意不用可变对象做输出参数，
   避免「忘了检查返回值」这类静默错误。

🔒 顺序纪律：本模块**不决定顺序**，顺序由 `pipeline` 的调用序列体现，
   而该序列必须与 `algo.config.VALIDATE_ORDER` 一致（`verify_ep4.py` 有守卫）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.algo import config as algo_config
from app.core import config as app_config
from app.core.errors import BizError, ErrorCode
from app.validators.constants import (
    ALLOWED_SUFFIX,
    CJK_RE,
    EN_COLUMN_HINT,
    MAX_ERROR_ROWS_EXPORT,
    MAX_ERROR_ROWS_RETURNED,
)
from app.validators.csv_io import count_data_rows, parse_dates, read_header
from app.validators.models import Inspection, Issue
from app.validators.stats import base_stats

__all__ = [
    "check_extension",
    "check_size",
    "check_structure",
    "check_row_limit",
    "check_dates",
    "check_sales",
    "check_series_limit",
]

logger = logging.getLogger(__name__)

VALIDATE_ORDER = algo_config.VALIDATE_ORDER


# ---------------------------------------------------------------------------
# 第 1 步 · 扩展名（落盘前即拒 → 不建行）
# ---------------------------------------------------------------------------
def check_extension(original_name: str | None) -> None:
    """`FR-UPL-01`：扩展名须为 `.csv`，否则 `40009`、落盘前拒绝。

    ⚠️ 这是**唯一一个抛异常而不是返回 `Inspection`** 的步骤，因为它在
    「文件落盘之前」执行，此时还没有任何巡检结果可承载 —— 直接抛 `BizError`
    让路由层统一转成 `40009`，比造一个空 `Inspection` 再判空更直白。
    """
    name = (original_name or "").strip()
    if not name:
        raise BizError(
            ErrorCode.BAD_EXTENSION,
            "未获取到文件名，无法判断文件类型",
            {"fail_stage": VALIDATE_ORDER[0], "suggestion": "请重新选择文件后再上传"},
        )
    if not name.lower().endswith(ALLOWED_SUFFIX):
        suffix = Path(name).suffix or "（无扩展名）"
        raise BizError(
            ErrorCode.BAD_EXTENSION,
            f"不支持的文件类型：{suffix}，仅支持 .csv 文件",
            {
                "fail_stage": VALIDATE_ORDER[0],
                "suggestion": "请在 Excel / 表格软件中「另存为 CSV（逗号分隔）」后重新上传",
            },
        )



# ---------------------------------------------------------------------------
# 第 2 步 · 大小 / 行数
# ---------------------------------------------------------------------------
def check_size(*, size_bytes: int, original_rows: int | None) -> Inspection | None:
    """字节数与「落盘前已知行数」的上限判定。"""
    settings = app_config.settings
    limit_bytes = settings.max_file_size_mb * 1024 * 1024
    stage = VALIDATE_ORDER[1]

    if size_bytes > limit_bytes:
        return Inspection(
            issue=Issue(
                ErrorCode.FILE_TOO_LARGE,
                stage,
                f"文件大小 {size_bytes / 1024 / 1024:.1f} MB 超过上限 "
                f"{settings.max_file_size_mb} MB",
                {
                    "fail_stage": stage,
                    "suggestion": (
                        f"请按时间或商品拆分后分批上传，单文件不超过 "
                        f"{settings.max_file_size_mb} MB"
                    ),
                },
            )
        )
    max_rows = settings.max_rows
    if original_rows is not None and original_rows > max_rows:
        return Inspection(
            issue=Issue(
                ErrorCode.ROW_OVER_LIMIT,
                stage,
                f"数据行数 {original_rows:,} 超过上限 {max_rows:,}",
                {
                    "fail_stage": stage,
                    "series_limit": settings.max_series,
                    "suggestion": (
                        f"请按时间或商品拆分后分批上传，单文件不超过 {max_rows:,} 行"
                        "（也可先按销量 Top 商品筛选）"
                    ),
                },
            )
        )
    return None


def check_row_limit(rows: int) -> Inspection | None:
    """解析后按真实行数复核一次上限。

    ⚠️ 为什么第 2 步判过还要再判：`count_data_rows()` 是按 `\\n` 计数，
       CSV 的字段里含引号换行时它会**偏小**，只能作为「快速失败」；
       真正可信的行数来自解析结果。少了这一道，含引号换行的大文件会漏网。
    """
    settings = app_config.settings
    if rows <= settings.max_rows:
        return None
    stage = VALIDATE_ORDER[1]
    return Inspection(
        issue=Issue(
            ErrorCode.ROW_OVER_LIMIT,
            stage,
            f"数据行数 {rows:,} 超过上限 {settings.max_rows:,}",
            {
                "fail_stage": stage,
                "suggestion": (
                    f"请按时间或商品拆分后分批上传，单文件不超过 {settings.max_rows:,} 行"
                ),
            },
        )
    )


# ---------------------------------------------------------------------------
# 第 2 步（尾）+ 第 3 步 · 表头与结构
# ---------------------------------------------------------------------------
def check_structure(
    path, encoding: str
) -> tuple[list[str] | None, Inspection | None]:
    """读表头 → 空文件判定 → 行数复核 → 缺列判定。

    :return: `(表头列名, 失败 Inspection | None)`
    """
    settings = app_config.settings
    stage = VALIDATE_ORDER[1]

    try:
        header = read_header(path, encoding)
    except Exception as exc:  # noqa: BLE001 - pd.errors.* 种类多，统一降级为「空文件」
        logger.info("表头读取失败，按空文件处理：%s", exc)
        header = []

    if not header:
        return None, Inspection(
            issue=Issue(
                ErrorCode.EMPTY_FILE,
                stage,
                "文件中没有可读取的表头",
                {
                    "fail_stage": stage,
                    "suggestion": "请确认文件非空且首行为表头（sku_id,warehouse_id,date,sales）",
                },
            )
        )

    data_rows = count_data_rows(path)
    if data_rows == 0:
        return None, Inspection(
            issue=Issue(
                ErrorCode.EMPTY_FILE,
                stage,
                "文件中没有数据行（仅有表头或为空白文件）",
                {
                    "fail_stage": stage,
                    "suggestion": "请补充至少一行历史销量数据后重新上传",
                },
            )
        )
    if data_rows > settings.max_rows:
        return None, Inspection(
            issue=Issue(
                ErrorCode.ROW_OVER_LIMIT,
                stage,
                f"数据行数 {data_rows:,} 超过上限 {settings.max_rows:,}",
                {
                    "fail_stage": stage,
                    "suggestion": (
                        f"请按时间或商品拆分后分批上传，单文件不超过 {settings.max_rows:,} 行"
                    ),
                },
            )
        )

    # 🔒 第 3 步：仓库列缺失即拒（FR-UPL-05），且提示必须点明英文列名（JT-01）
    missing = [c for c in algo_config.REQUIRED_COLUMNS if c not in header]
    if not missing:
        return header, None

    chinese = [c for c in header if CJK_RE.search(c)]
    extra = f"；检测到中文列名：{'、'.join(chinese[:6])}" if chinese else ""
    return None, Inspection(
        issue=Issue(
            ErrorCode.MISSING_COLUMN,
            VALIDATE_ORDER[2],
            f"缺少必需字段：{'、'.join(missing)}{extra}",
            {
                "fail_stage": VALIDATE_ORDER[2],
                "missing_columns": missing,
                "suggestion": (
                    f"{EN_COLUMN_HINT}。当前表头：{'、'.join(header[:8])}"
                    + ("（…）" if len(header) > 8 else "")
                ),
            },
        ),
        error_columns=header,
    )


# ---------------------------------------------------------------------------
# 第 4 步 · 日期可解析性
# ---------------------------------------------------------------------------
def check_dates(
    df: pd.DataFrame, columns: list[str], row_no: np.ndarray
) -> tuple[pd.Series | None, Inspection | None]:
    """日期列能否解析（`FR-UPL-09`）。

    :return: `(解析后的日期列, 失败 Inspection | None)`
    """
    parsed = parse_dates(df[algo_config.COL_DATE])
    bad = parsed.isna()
    if not bool(bad.any()):
        return parsed, None

    idx = np.flatnonzero(bad.to_numpy())
    samples = idx[:MAX_ERROR_ROWS_EXPORT]
    unparsable = [
        {
            "row_no": int(row_no[i]),
            "raw_value": str(df[algo_config.COL_DATE].iloc[i]),
            "column": algo_config.COL_DATE,
        }
        for i in samples
    ]
    error_rows = [
        {
            **{c: str(df[c].iloc[i]) for c in columns},
            "row_no": int(row_no[i]),
            "error_reason": "日期无法解析",
        }
        for i in samples
    ]
    return None, Inspection(
        issue=Issue(
            ErrorCode.DATE_UNPARSABLE,
            VALIDATE_ORDER[3],
            f"日期列存在 {int(bad.sum()):,} 个无法解析的值",
            {
                "fail_stage": VALIDATE_ORDER[3],
                "unparsable_rows": unparsable[:MAX_ERROR_ROWS_RETURNED],
                "suggestion": (
                    "支持格式为 YYYY-MM-DD（推荐），也可识别 2015/1/7、20151010 等写法。"
                    f"请修正上述行后重新上传。仅展示前 {MAX_ERROR_ROWS_RETURNED} 条，"
                    "完整明细可下载错误明细 CSV"
                ),
            },
            error_rows=error_rows,
        ),
        stats={"row_count": int(len(df))},
        error_columns=columns,
    )


# ---------------------------------------------------------------------------
# 第 5 步 · 销量非负
# ---------------------------------------------------------------------------
def check_sales(
    df: pd.DataFrame, columns: list[str], row_no: np.ndarray
) -> tuple[pd.Series | None, Inspection | None]:
    """销量列非负且可转数值（`FR-UPL-12`/`FR-UPL-09`）。

    ⚠️ 「非数值/空」与「负值」共用 `40003`：两者都是「sales 这一列的值不合法」，
       修复动作相同（改这一列）；拆成两个码只会让前端分支变多。

    :return: `(数值化后的销量列, 失败 Inspection | None)`
    """
    sales = pd.to_numeric(df[algo_config.COL_SALES], errors="coerce")
    non_numeric = sales.isna()
    negative = (~non_numeric) & (sales < 0)
    if not (bool(non_numeric.any()) or bool(negative.any())):
        return sales, None

    idx = np.flatnonzero((non_numeric | negative).to_numpy())
    samples = idx[:MAX_ERROR_ROWS_EXPORT]
    negative_rows = [
        {"row_no": int(row_no[i]), "value": float(sales.iloc[i])}
        for i in samples
        if not bool(non_numeric.iloc[i])
    ]
    non_numeric_rows = [
        {
            "row_no": int(row_no[i]),
            "raw_value": str(df[algo_config.COL_SALES].iloc[i]),
            "column": algo_config.COL_SALES,
        }
        for i in samples
        if bool(non_numeric.iloc[i])
    ]
    error_rows = [
        {
            **{c: str(df[c].iloc[i]) for c in columns},
            "row_no": int(row_no[i]),
            "error_reason": (
                "销量非数值或为空" if bool(non_numeric.iloc[i]) else "销量为负值"
            ),
        }
        for i in samples
    ]
    parts = []
    if len(negative_rows):
        parts.append(f"{int(negative.sum()):,} 个负值")
    if len(non_numeric_rows):
        parts.append(f"{int(non_numeric.sum()):,} 个非数值 / 空值")
    payload: dict[str, Any] = {
        "fail_stage": VALIDATE_ORDER[4],
        "negative_rows": negative_rows[:MAX_ERROR_ROWS_RETURNED],
        "unparsable_rows": non_numeric_rows[:MAX_ERROR_ROWS_RETURNED],
        "suggestion": (
            "系统不会自动取绝对值、也不会自动补 0，请修正数据后重新上传"
            "（负销量通常源于退货未冲销）。完整明细可下载错误明细 CSV"
        ),
    }
    return None, Inspection(
        issue=Issue(
            ErrorCode.NEGATIVE_VALUE,
            VALIDATE_ORDER[4],
            f"销量列存在 {' 与 '.join(parts)}",
            payload,
            error_rows=error_rows,
        ),
        stats={"row_count": int(len(df))},
        error_columns=columns,
    )


# ---------------------------------------------------------------------------
# 第 7 步 · 序列数上限
# ---------------------------------------------------------------------------
def check_series_limit(
    *, series_count: int, gap_count: int, df: pd.DataFrame, columns: list[str]
) -> Inspection | None:
    """序列数上限（`FR-UPL-11`）。

    ⚠️ **有断档时不在这里失败**：断档要先让用户选择「插值 / 跳过」
       （`FR-UPL-13`），选完再校验序列数。若此刻就按序列数拒绝，
       用户连做选择的入口都拿不到。
    """
    settings = app_config.settings
    if gap_count != 0 or series_count <= settings.max_series:
        return None
    return Inspection(
        issue=Issue(
            ErrorCode.SERIES_OVER_LIMIT,
            VALIDATE_ORDER[6],
            f"待预测序列数为 {series_count:,}，超过上限 {settings.max_series:,}",
            {
                "fail_stage": VALIDATE_ORDER[6],
                "series_count": series_count,
                "series_limit": settings.max_series,
                "suggestion": (
                    f"请在上传前筛选商品，将序列数控制在 {settings.max_series:,} 以内"
                    f"（当前超出 {series_count - settings.max_series:,} 条）。"
                    "序列数 = sku_id × warehouse_id 去重后的组合数"
                ),
            },
        ),
        stats=base_stats(df, series_count),
        error_columns=columns,
    )
