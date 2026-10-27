"""CSV 注入防护（Excel DDE 公式执行）。

为什么要有这个模块（2026-10-26 复评发现）
-----------------------------------------------------------------------------
用户上传的 CSV 里，`sku_id` / `warehouse_id` / `date` 都是**任意文本**。
只要某个单元格以 `=` `+` `-` `@` 或 Tab/CR 开头，Excel 打开时就会
把它当公式执行（DDE / `=cmd|' /c calc'!A0`）—— 这是**下载即中招**，
受害者是下载自己数据的业务人员。

🔴 **两个出口，威胁模型完全相同，之前只防了一个**：
- `API-16` 结果导出（`result_service`）→ 已防；
- `API-11` 错误明细 CSV（`dataset_service`）→ **漏了**，
  而它下载的恰恰是**用户上传的原始行**（含原始 `sku_id`）。

所以把防护提到 `app/core/` 里，让两个出口共用同一份实现 ——
否则下一个新出口还会再漏一次。

🔒 **只转义文本列，绝不转义数值列**：
数值列的 `-12` 也以 `-` 开头，一旦加了前缀就变成 `'-12`，
Excel 会把它当文本，聚合与作图全错。判定靠**列名**（`COL_SALES`），
不靠「值长得像不像数字」—— 用户可以上传 `sales = "-1e5"` 这种
看着像数字、实际是字符串的恶意值。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 以这些字符开头的**文本**单元格会被 Excel 当公式执行。
UNSAFE_PREFIX: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: Any) -> str:
    """把一个文本单元格转义成不会被 Excel 当公式执行的形式。

    做法是在开头加一个单引号（Excel 的「强制文本」前缀）。
    数值列**不要**调用本函数。
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in UNSAFE_PREFIX else text


def escape_row(
    row: Mapping[str, Any],
    fieldnames: Iterable[str],
    *,
    numeric_columns: Iterable[str] = (),
) -> dict[str, str]:
    """把一整行转义成可写入 CSV 的字典。

    :param numeric_columns: **数值列的列名**（这些列原样输出，不加前缀）。
    """
    numeric = {str(c) for c in numeric_columns}
    out: dict[str, str] = {}
    for key in fieldnames:
        raw = row.get(key, "")
        if key in numeric:
            out[key] = "" if raw is None else str(raw)
        else:
            out[key] = safe_text(raw)
    return out
