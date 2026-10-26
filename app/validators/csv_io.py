"""CSV 读取与值转换（从 `dataset_service` 拆出，`R-06`）。

📌 独立成模块的理由：这 7 个函数**不属于任何一步校验**，而是被校验、断档处置、
   预览、错误明细等多条链路共用。留在 `dataset_service` 里的后果是——想拆
   `inspect_file()` 就必须把它一起搬走，否则新模块要反过来 import 业务模块，
   形成 `validators ← services` 的循环。
"""
from __future__ import annotations

import math
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

__all__ = [
    "detect_encoding",
    "count_data_rows",
    "load_csv",
    "read_header",
    "parse_dates",
    "to_number",
    "fmt_num",
]


def detect_encoding(path: Path) -> str:
    """探测编码：优先 UTF-8（含 BOM），回退 GB18030（国内 Excel 另存常见）。

    ⚠️ 只探测文件头 64 KB。GB18030 能解码绝大多数字节序列，因此**必须**
       先试 UTF-8，否则中文文件会被误判成 GB18030 而出现乱码式「解析成功」。
    """
    head = path.open("rb").read(1 << 16)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            head.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def count_data_rows(path: Path) -> int:
    """快速统计数据行数（不含表头）。

    用二进制块计数而不是 `pd.read_csv` —— 第 2 步的目的是「越靠前越快失败」，
    没必要为了一次行数检查把 20 MB 全部解析成 DataFrame。
    """
    newlines = 0
    tail = b""
    with path.open("rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            newlines += block.count(b"\n")
            tail = block[-1:]
    lines = newlines if tail in (b"", b"\n") else newlines + 1
    return max(lines - 1, 0)


def load_csv(path: Path, encoding: str | None = None) -> tuple[pd.DataFrame, str]:
    """整表读入（`dtype=str` + `keep_default_na=False`，保留原始字面值）。

    🔒 全列保留：`FR-UPL-06` 要求错误明细「保留原始列」，故不能只读必需 4 列。
    """
    enc = encoding or detect_encoding(path)
    kwargs: dict[str, Any] = {
        "dtype": str,
        "keep_default_na": False,
        "encoding": enc,
        "skipinitialspace": True,
    }
    try:
        return pd.read_csv(path, **kwargs), enc
    except UnicodeDecodeError:
        # 文件头是纯 ASCII、正文却是 GB 系编码时的兜底
        if enc == "utf-8-sig":
            kwargs["encoding"] = "gb18030"
            return pd.read_csv(path, **kwargs), "gb18030"
        raise


def read_header(path: Path, encoding: str) -> list[str]:
    """只读表头（`nrows=0`），列名去 BOM 与首尾空白。"""
    df = pd.read_csv(
        path, nrows=0, dtype=str, encoding=encoding, skipinitialspace=True
    )
    return [str(c).strip().lstrip("\ufeff") for c in df.columns]


def parse_dates(series: pd.Series) -> pd.Series:
    """日期解析：先按推荐的 `YYYY-MM-DD`，再对剩余项做宽松解析。

    PRD §3.2.2 的约束是「**可解析**，推荐 `YYYY-MM-DD`」，因此
    `2015/1/7`、`20151010` 这类写法**应当放行**；只有真正无法解析的值
    （`2015/13/45`、`未知`、空串）才计入 `unparsable_rows`。
    """
    strict = pd.to_datetime(series, format="%Y-%m-%d", errors="coerce")
    bad = strict.isna()
    if bad.any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            loose = pd.to_datetime(
                series[bad].astype(str).str.strip(), format="mixed", errors="coerce"
            )
        strict = strict.copy()
        strict.loc[bad] = loose
    return strict


def to_number(raw: Any) -> Any:
    """把原始字面值转成「预览用」的数值：整数优先，其次浮点，失败则原样返回。"""
    text = str(raw).strip()
    if text == "":
        return ""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return raw
    return int(value) if float(value).is_integer() else value


def fmt_num(value: float) -> str:
    """写 CSV 用的数值格式化：整数不带小数点，小数最多 4 位且不留尾零。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")
