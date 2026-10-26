"""预测结果与明细（`API-14`）。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from app.schemas.common import AppBase

__all__ = [
    "MetricBundle",
    "Coverage",
    "Distribution",
    "TimingMs",
    "SeriesCurve",
    "PredictionRow",
    "TaskBrief",
    "DatasetBrief",
    "PredictionResultResponse",
    "DetailQuery",
    "ExportQuery",
    "DETAIL_SORT_COLUMNS",
    "DETAIL_LEVELS",
]

# =============================================================================
# 5. 预测结果                                          API-14
# =============================================================================


class MetricBundle(AppBase):
    """与 outputs/eval_report.json 的 metrics 段一一对应，保证同源可核对。

    🔒 **除 `series_evaluated` / `series_passed` 外全部允许为 null**。
       这不是宽松设计，而是必需：`compute_task_metrics` 在**没有任何可评估
       序列**时（`levels_for_main=("L1",)` 且 L1 数量为 0 —— 例如用户上传的
       数据全是 5 天的短序列，全部落入 L2/L3）会把所有误差指标置为 NaN，
       落库时经 `_f()` 转成 NULL。此时若声明成 `float`，pydantic 会抛
       ValidationError → `API-14` 返回 500，**整个结果页白屏**。

       ⚠️ 这条分支在示例数据集上永远走不到（L1=81），只有上传稀疏数据
          才会触发 —— `verify_ep6_api.py` 的 ⑥ 用例专门构造它。

       前端契约：拿到 null 时显示「—」并把该指标判为「无法评估」，
       **不得**显示 0（0 会被读成「误差为零」，比空白更糟）。
    """

    weighted_mape: float | None = None
    weighted_mae: float | None = None
    weighted_rmse: float | None = None
    baseline_mape: float | None = None
    baseline_mae: float | None = None
    baseline_rmse: float | None = None
    mae_improve_pct: float | None = None
    rmse_improve_pct: float | None = None
    series_evaluated: int
    series_passed: int
    series_pass_rate: float | None = None
    l1_mape: float | None = None
    l2_mape: float | None = None


class Coverage(AppBase):
    """三项计数之和必须等于 total（UI/UX UX-AC-06、FR-VIS-07）。"""

    L1: int
    L2: int
    L3: int
    total: int
    L1_pct: float
    L2_pct: float
    L3_pct: float
    predictable: int
    predictable_pct: float


class Distribution(AppBase):
    """逐序列 MAPE 分布。

    🔒 分位数允许为 null：没有可评估序列时 `series_mape_distribution` 返回
       全 NaN，落库为 NULL。同样不可声明为 `float`（见 `MetricBundle` 说明）。
    """

    median: float | None = None
    p75: float | None = None
    p90: float | None = None
    max: float | None = None
    over_count: int
    threshold: float = 20.0
    buckets: list[int] | None = None


class TimingMs(AppBase):
    preprocess_ms: int
    features_ms: int
    inference_ms: int
    metrics_ms: int
    total_ms: int


class SeriesCurve(AppBase):
    key: str
    sku_id: str
    warehouse_id: str
    granularity_level: Literal["L1", "L2", "L3"]
    mape: float | None = None
    passed: bool = False
    forecast_dates: list[str]
    # 🔒 `predicted` / `baseline` 的元素允许为 null —— 这**不是**可选，是必需：
    #    L3 序列在 `predictions` 表里只落一行占位，且 `baseline_value` 恒为 NULL
    #    （见 `models/prediction.py` 与 `predict_service._build_prediction_rows`）。
    #    若声明成 `list[float]`，pydantic 会因 `None` 抛 ValidationError →
    #    `API-14` 直接 500，**整个结果页打不开**。
    #    ⚠️ 这个缺陷在示例数据集上永远看不见（L3 = 0），只有上传稀疏数据
    #    触发 L3 才会暴露 —— 故 `verify_ep6_api.py` 的 ⑥ 用例专门构造它。
    predicted: list[float | None]
    actual: list[float] | None = None
    baseline: list[float | None]
    history_dates: list[str]
    history_sales: list[float]


class PredictionRow(AppBase):
    sku_id: str
    warehouse_id: str
    forecast_date: str
    predicted_value: float
    actual_value: float | None = None
    baseline_value: float | None = None
    granularity_level: Literal["L1", "L2", "L3"]
    error: float | None = None
    error_pct: float | None = None


class TaskBrief(AppBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_name: str | None = None
    status: str
    horizon: int
    granularity: str = "warehouse_sku"
    model_version: str
    device: str
    device_fallback: int = 0
    series_total: int
    duration_ms: int
    created_at: str
    finished_at: str | None = None
    # 🔒 EP-7 补充：失败任务专有三字段（`API-19` 差异表）。
    #    历史详情页要靠它们告诉用户「为什么失败、卡在哪一步」。
    #    成功任务为 null —— **不可省**：pydantic 会丢弃 schema 未声明的字段，
    #    服务层返回了但响应里没有，前端拿到 undefined，看起来像「后端没存」。
    error_code: str | None = None
    error_msg: str | None = None
    completed_stage: str | None = None


class DatasetBrief(AppBase):
    id: int
    file_name: str
    row_count: int
    sku_count: int
    warehouse_count: int
    date_start: str
    date_end: str


class PredictionResultResponse(AppBase):
    task: TaskBrief
    dataset: DatasetBrief
    metrics: MetricBundle
    coverage: Coverage
    # 🔒 可空：失败任务（API-19 差异表）没有分布。
    #    声明非空会让 pydantic 校验失败 → 历史详情页整页 500。
    distribution: Distribution | None = None
    timing_ms: TimingMs
    is_over_threshold: bool
    eval_note: str = Field(
        ...,
        description="评估前提，必须原样展示（FR-MOD-13）："
        "本结果使用预训练模型推理，未针对本次输入数据微调。",
    )
    series: list[SeriesCurve] = Field(default_factory=list)
    detail_total: int = 0
    # 🔒 API-14-3：曲线被 series_limit 截断时为 True。
    #    前端据此提示「仅展示前 N 条」，避免用户误以为「只有这些序列」。
    truncated: bool = False


# 🔒 API-15 排序列白名单（FR-VIS-03「按任意列排序」的合法取值域）。
#    与 app/dao/prediction_dao.py::SORTABLE_COLUMNS 的键集必须一致 ——
#    这里是**对外契约**（决定前端表格有哪些列可排序、OpenAPI 文档写什么），
#    那里是**运行时取值**。两处不一致时，前端能点但后端静默退回默认列。
DETAIL_SORT_COLUMNS = ("sku_id", "warehouse_id", "forecast_date", "predicted_value", "error")
DETAIL_LEVELS = ("L1", "L2", "L3")


class DetailQuery(AppBase):
    """`API-15` 查询参数。

    ⚠️ 排序白名单在此**再校验一次**（DAO 层也有一份）。不是冗余：
    DAO 那份防的是注入与 AttributeError，这里防的是「契约外的值被静默接受」——
    前端传 `sort_by=granularity_level` 时，若 schema 允许而 DAO 不认，
    用户点了排序却发现顺序没变，且没有任何提示。
    """

    page: int = Field(1, ge=1, description="页码，从 1 开始")
    size: int = Field(20, ge=1, le=100, description="每页条数，上限 100（FR-VIS-03 默认 20）")
    sort_by: str = Field("sku_id", description=f"排序列，取值：{DETAIL_SORT_COLUMNS}")
    order: str = Field("asc", pattern="^(asc|desc)$", description="排序方向")
    level: str | None = Field(None, description=f"分级过滤：{DETAIL_LEVELS}")
    keyword: str | None = Field(None, max_length=64, description="商品 / 仓库模糊搜索")

    @field_validator("sort_by")
    @classmethod
    def _check_sort(cls, v: str) -> str:
        if v not in DETAIL_SORT_COLUMNS:
            raise ValueError(
                f"不支持按 {v} 排序，可用列：{'、'.join(DETAIL_SORT_COLUMNS)}"
            )
        return v

    @field_validator("level")
    @classmethod
    def _check_level(cls, v: str | None) -> str | None:
        if v is not None and v not in DETAIL_LEVELS:
            raise ValueError(f"分级只能是 {'/'.join(DETAIL_LEVELS)} 之一")
        return v


class ExportQuery(AppBase):
    """`API-16` 查询参数。"""

    format: str = Field(
        "csv",
        pattern="^(csv|xlsx)$",
        description="导出格式：csv（带 UTF-8 BOM）/ xlsx",
    )
