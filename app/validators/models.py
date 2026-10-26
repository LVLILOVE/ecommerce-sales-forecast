"""校验结果的数据结构（`Issue` / `Inspection`）。

从 `app/services/dataset_service.py` 拆出（`R-06`，2026-10-26）。
放在独立模块的理由：**校验流程（`validators`）与数据集业务（`services`）要能各自
import 这两个类型而不互相依赖** —— 否则拆包只能拆成「循环 import」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Issue", "Inspection"]


@dataclass
class Issue:
    """一次校验失败的完整描述（错误码 + 阶段 + 定位载荷）。"""

    code: int
    fail_stage: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    # 写「错误明细 CSV」用；为空表示该错误没有行级明细（如缺列）
    error_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Inspection:
    """一次文件巡检的结果。`issue` 非空即失败（按 VALIDATE_ORDER 的第一个失败项）。"""

    issue: Issue | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    preview: dict[str, Any] = field(default_factory=dict)
    gap_ranges: list[dict[str, Any]] = field(default_factory=list)
    gap_count: int = 0
    gap_days_total: int = 0
    error_columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.issue is None
