"""数据集（`API-06` ~ `API-11`）。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.schemas.common import MAX_SERIES, AppBase

__all__ = [
    "DatasetSummary",
    "DatasetPreview",
    "GapWarning",
    "UploadDatasetResponse",
    "ResolveGapRequest",
    "LevelPreview",
    "HorizonEstimate",
    "DatasetDetailResponse",
]

# =============================================================================
# 3. 数据集                                            API-06 ~ API-11
# =============================================================================


class DatasetSummary(AppBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_name: str
    file_size: int
    row_count: int
    sku_count: int
    warehouse_count: int
    valid_series_count: int
    date_start: str
    date_end: str
    gap_filled: int = 0
    gap_count: int = 0
    is_sample: int = 0
    status: int = 1
    created_at: str


class DatasetPreview(AppBase):
    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    shown: int


class GapWarning(AppBase):
    has_gap: bool
    gap_count: int = 0
    ranges: list[dict[str, Any]] = Field(default_factory=list)
    interpolated: bool = False


class UploadDatasetResponse(AppBase):
    dataset: DatasetSummary
    preview: DatasetPreview
    series_count: int
    series_limit: int = MAX_SERIES
    gap_warning: GapWarning


class ResolveGapRequest(AppBase):
    """FR-UPL-13：断档处置必须由用户显式选择，系统不得自动继续。"""

    dataset_id: int
    action: Literal["interpolate", "abort"]


class LevelPreview(AppBase):
    """序列三级分级预判（`FR-FEA-09`）—— 参数页右栏的数据源（`API-09`）。

    字段名保持 `L1/L2/L3` 大写：与 `algo/classify.py` 的 `LevelCounts`、
    前端 `types.ts` 以及原型 `params.html` 的展示文案四处同名同源。
    """

    L1: int
    L2: int
    L3: int
    total: int


class HorizonEstimate(AppBase):
    """某一预测步长下的端到端预估耗时（`API-09` / `UX-P06-06`）。

    ⚠️ 与 `estimated_ms` 是**两个不同的量**，不可互相替代：
      - `estimated_ms`           = 单遍算法评测（`eval_report.timing_ms`）按序列折算，
                                   用来与「算法基准」对照，`EP-3`/`EP-4` 已钉住；
      - `estimate_by_horizon[*]` = **真实任务**的端到端预估（两遍流水线 + 逐序列
                                   滚动 + 落库），随步长变化。参数页展示的是后者。
    """

    horizon: int
    estimated_ms: int


class DatasetDetailResponse(AppBase):
    """`API-09` 响应：数据集概要 + 分级预判 + 耗时预估。"""

    dataset: DatasetSummary
    level_preview: LevelPreview
    estimated_ms: int
    # 序列数 > 800 时置 true（`API-09`）——前端据此提示「已接近 60 秒预算」
    over_budget_risk: bool
    # 🆕 EP-5：按步长给出的端到端预估（见 HorizonEstimate 的说明）。
    #    默认空列表，保证老调用方（只读前四个字段）不受影响。
    estimate_by_horizon: list[HorizonEstimate] = Field(default_factory=list)
