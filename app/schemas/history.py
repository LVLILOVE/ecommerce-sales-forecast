"""历史与对比（`API-16` ~ `API-19`）。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import AppBase
from app.schemas.result import Coverage, MetricBundle

__all__ = [
    "HistoryQuery",
    "HistoryItem",
    "HistoryPage",
    "CompareRequest",
    "CompareSeries",
    "CompareTaskSlot",
    "CompareResponse",
    "WorkbenchSummary",
]

# =============================================================================
# 6. 历史与对比                                        API-16 ~ API-19
# =============================================================================


class HistoryQuery(AppBase):
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    keyword: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    # ⚠️ 不在此处收窄取值域：非法 status 应由服务层返业务码 40012
    #    （HTTP 200 + body.code），而不是被 FastAPI 拦成 422 —— 本项目
    #    的错误契约是「除 401 外一律 HTTP 200」。
    status: str | None = None


class HistoryItem(AppBase):
    """对应数据库视图 v_task_summary，避免前端多次拼接。"""

    task_id: int
    task_name: str | None = None
    status: str
    granularity: str
    horizon: int
    # 🔒 EP-7 补充：dataset_id 是「原参数重试」入口的必需字段
    #    （API-19-1）。视图原先只给 dataset_name（展示用），
    #    前端要拿 id 就得多查一次详情。
    dataset_id: int | None = None
    dataset_name: str
    series_total: int
    duration_ms: int | None = None
    device: str | None = None
    device_fallback: int = 0
    model_version: str
    weighted_mape: float | None = None
    weighted_mae: float | None = None
    weighted_rmse: float | None = None
    mae_improve_pct: float | None = None
    rmse_improve_pct: float | None = None
    series_pass_rate: float | None = None
    is_over_threshold: int | None = None
    error_code: str | None = None
    created_at: str
    finished_at: str | None = None


class HistoryPage(AppBase):
    """`API-17` 响应体。"""

    items: list[HistoryItem] = Field(default_factory=list)
    page: int = 1
    size: int = 20
    total: int = 0
    pages: int = 0


class CompareRequest(AppBase):
    task_ids: list[int]
    sku_ids: list[str] | None = None
    warehouse_ids: list[str] | None = None

    @field_validator("task_ids")
    @classmethod
    def check_count(cls, v: list[int]) -> list[int]:
        if not 2 <= len(v) <= 4:
            raise ValueError("跨任务对比需选择 2 至 4 个任务")
        if len(set(v)) != len(v):
            raise ValueError("对比任务不得重复选择")
        return v


class CompareSeries(AppBase):
    """`API-18` 对比曲线。

    🔒 **刻意复用不了 `SeriesCurve`**：那条曲线带 `history_dates` /
       `history_sales` / `baseline` / `actual`，而对比页只画预测值。
       若硬塞进 `SeriesCurve`，要么为省流量传空数组（声明非空 → 校验失败），
       要么让 4 个任务各读一遍 CSV 历史段（`API-18` 的 ≤ 3 s 预算直接爆）。
    """

    key: str
    granularity_level: Literal["L1", "L2", "L3"]
    forecast_dates: list[str]
    predicted: list[float | None]


class CompareTaskSlot(AppBase):
    task_id: int
    task_name: str | None = None
    horizon: int
    dataset_id: int | None = None
    dataset_name: str
    created_at: str
    # 🔒 可空：失败任务不参与曲线对比，指标与覆盖率均不存在。
    #    声明为非空会让 pydantic 校验失败 → 对比页整页 500。
    status: str = "succeeded"
    duration_ms: int | None = None
    metrics: MetricBundle | None = None
    coverage: Coverage | None = None
    series: list[CompareSeries] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CompareResponse(AppBase):
    tasks: list[CompareTaskSlot]
    attribution: list[dict[str, Any]] = Field(default_factory=list)
    dataset_consistent: bool = True
    # 🔒 API-18-2 的 ②：所有成功任务预测日期的交集。
    #    为空数组时前端**只画指标表不画曲线** —— 强行按索引对齐
    #    会画出「8 月的预测对着 11 月真值」的假对比。
    overlap_dates: list[str] = Field(default_factory=list)
    # 🔒 跨任务一致性警告的汇总副本（步长不一致 / 数据集不一致 / 有失败任务）。
    #    各任务的 `warnings` 是逐条归因，这里是页面顶部的整块告警。
    warnings: list[str] = Field(default_factory=list)


class WorkbenchSummary(AppBase):
    """`UX-P04` 工作台概览。"""

    is_guest: bool = False
    stats: dict[str, Any] = Field(default_factory=dict)
    recent: list[HistoryItem] = Field(default_factory=list)
    has_dataset: bool = False
    has_model: bool = False
