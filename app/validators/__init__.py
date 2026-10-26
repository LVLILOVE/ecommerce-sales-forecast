"""文件校验包（7 步校验，`R-06` 从 `dataset_service` 拆出）。

为什么单独成包
-----------------------------------------------------------------------------
`dataset_service.py` 曾经 1,366 行、其中 `inspect_file()` 一个函数 273 行 ——
「第 8 步校验加不进去」不是感受而是事实：想加第 8 步，必须先读懂前面 7 段的
局部变量（`df` / `columns` / `row_no` / `parsed_date` / `sales` …）在哪个分支被
赋值、哪些分支提前 `return` 了。这种「一个函数内串联 7 段流程 + 共享 6 个局部变量」
的写法，是典型的「加一步就多一处回归风险」。

拆分后职责边界
-----------------------------------------------------------------------------
| 模块 | 职责 | 加第 8 步时要不要改 |
|---|---|---|
| `models.py` | `Issue` / `Inspection` 两个数据结构 | 否 |
| `constants.py` | 阈值与提示文案 | 否 |
| `csv_io.py` | 编码探测 / 读表 / 读表头 / 日期与数值转换 | 否 |
| `stats.py` | 统计摘要 / 断档检测 / 预览 | 否 |
| `steps.py` | **一步一个函数**，只判定 + 造 `Issue` | **新增一个 `check_xxx()`** |
| `pipeline.py` | 编排：按 `VALIDATE_ORDER` 顺序调用、短路返回 | **插一行** |

依赖方向是单向的：`pipeline → steps → [csv_io, stats, constants, models]`，
没有任何回边。所以 `services/dataset_service.py` 可以放心 import 本包，
而本包**不 import** 任何 `services.*`（否则就退回循环依赖）。

🔒 与 `dataset_service` 的分工
- 本包只管「**这个文件能不能用**」，不知道 `datasets` 表、不知道用户、不知道事务；
- 落盘、写库、错误明细导出、断档处置仍归 `services/dataset_service.py`。
  这样校验逻辑可以脱离数据库被单测（`tests/test_validators.py` 就是这么做的）。
"""
from __future__ import annotations

from app.validators.constants import (
    ALLOWED_SUFFIX,
    CJK_RE,
    EN_COLUMN_HINT,
    MAX_ERROR_ROWS_EXPORT,
    MAX_ERROR_ROWS_RETURNED,
    MAX_GAP_RANGES_RETURNED,
    PREVIEW_ROWS,
)
from app.validators.csv_io import (
    count_data_rows,
    detect_encoding,
    fmt_num,
    load_csv,
    parse_dates,
    read_header,
    to_number,
)
from app.validators.models import Inspection, Issue
from app.validators.pipeline import inspect_file
from app.validators.steps import check_extension
from app.validators.stats import base_stats, build_preview, detect_gap_ranges

__all__ = [
    "ALLOWED_SUFFIX",
    "CJK_RE",
    "EN_COLUMN_HINT",
    "Inspection",
    "Issue",
    "MAX_ERROR_ROWS_EXPORT",
    "MAX_ERROR_ROWS_RETURNED",
    "MAX_GAP_RANGES_RETURNED",
    "PREVIEW_ROWS",
    "base_stats",
    "build_preview",
    "check_extension",
    "count_data_rows",
    "detect_encoding",
    "detect_gap_ranges",
    "fmt_num",
    "inspect_file",
    "load_csv",
    "parse_dates",
    "read_header",
    "to_number",
]
