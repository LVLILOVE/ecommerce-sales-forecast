"""校验相关的常量（从 `dataset_service` 拆出，`R-06`）。

⚠️ **`VALIDATE_ORDER` 不在这里**：它的唯一真源是 `app/algo/config.py`，
   本包与 `dataset_service` 都直接 import 它（两处都是 re-export，不是副本）。
   `verify_ep4.py` 的 ①-2/①-3 会盯着「有没有人另抄一份字面量」。
"""
from __future__ import annotations

import re

__all__ = [
    "ALLOWED_SUFFIX",
    "CJK_RE",
    "EN_COLUMN_HINT",
    "MAX_ERROR_ROWS_EXPORT",
    "MAX_ERROR_ROWS_RETURNED",
    "MAX_GAP_RANGES_RETURNED",
    "PREVIEW_ROWS",
]

ALLOWED_SUFFIX = ".csv"

MAX_GAP_RANGES_RETURNED = 50          # 响应里最多回传的断档区间数（避免大文件把响应撑爆）
MAX_ERROR_ROWS_RETURNED = 20          # FR-UPL-10：响应中最多列前 20 条
MAX_ERROR_ROWS_EXPORT = 500_000       # 错误明细 CSV 的行数上限（= MAX_ROWS，实务上等于全部）
PREVIEW_ROWS = 20                     # FR-UPL-07：预览前 20 行

# PRD §3.2.2 的英文列名提示（JT-01 要求「中文列名文件被拒」时明确指出这一点）
EN_COLUMN_HINT = (
    "本系统只接受英文列名 sku_id / warehouse_id / date / sales（区分大小写），"
    "不支持中文列名。请下载标准模板后按模板填写"
)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
"""检测中文列名用（`JT-01`：中文表头要给出「不支持中文列名」的具体提示）。"""
