"""预测任务（`API-12` ~ `API-15`）。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import ALLOWED_HORIZONS, SLA_SECONDS, AppBase

__all__ = [
    "CreateTaskRequest",
    "CreateTaskResponse",
    "TaskStatusResponse",
]

# =============================================================================
# 4. 预测任务                                          API-12 ~ API-15
# =============================================================================


class CreateTaskRequest(AppBase):
    dataset_id: int = Field(..., gt=0)
    granularity: Literal["warehouse_sku"] = "warehouse_sku"
    horizon: int
    use_holiday_feature: int = 1
    task_name: str | None = Field(None, max_length=64)

    @field_validator("horizon")
    @classmethod
    def check_horizon(cls, v: int) -> int:
        if v not in ALLOWED_HORIZONS:
            raise ValueError(f"预测步长仅支持 {list(ALLOWED_HORIZONS)} 天")
        return v

    @field_validator("use_holiday_feature")
    @classmethod
    def check_flag(cls, v: int) -> int:
        if v not in (0, 1):
            raise ValueError("use_holiday_feature 仅接受 0 或 1")
        return v


class CreateTaskResponse(AppBase):
    """`API-12` 响应。

    ⚠️ `EP-5` 契约修正（两处，均为「让响应能承载文档已要求的信息」）：

    ① **`status` 取值域扩为四态**。原文写 `Literal["created", "running"]`，
       但本接口是**同步执行**的（§5.6「📌 同步语义：60 秒内必然返回」），
       返回时任务已进入终态；且 `API-12-2` 明确要求「超预算时**返回** `code: 0`
       但**任务状态为 `failed`**」。若严格只允许两个值，pydantic 会拒绝
       `succeeded` / `failed` 的响应体，接口直接 500 —— 契约与自身语义打架。

    ② **补齐软失败与结果摘要字段**。文档 `API-12-2` 要求「在 `data` 中给出
       已完成阶段与建议」，`FR-MOD-10` 要求披露设备回退。这些字段此前没进模型，
       而 `AppBase` 走 pydantic 默认的 `extra='ignore'`，**多余键会被静默丢弃**
       —— 于是「任务失败的原因」在前端永远是空的。
    """

    task_id: int
    status: Literal["created", "running", "succeeded", "failed"]
    model_version: str
    series_count: int
    estimated_ms: int = Field(..., description="实测耗时（ms），前端进度条刻度依据")

    # ---- 成功时的结果摘要（可选；失败时为 None） ----
    device: Literal["cuda", "cpu"] | None = None
    device_fallback: int = 0
    coverage: dict[str, Any] | None = Field(
        None, description="覆盖率披露 {L1,L2,L3,total,L1_pct,...}，与 eval_report 同源"
    )
    mape: float | None = Field(None, description="本次任务的加权 MAPE（百分比）")
    duration_ms: int | None = None
    timing: dict[str, Any] | None = Field(
        None,
        description=(
            "🆕 EP-5：本次任务的真实分段耗时（与 metrics.timing_json 同源）。"
            "执行页 UX-P07-01 要求「一眼看出哪一段最贵」，而同步 API-12 下"
            "前端在任务运行期间拿不到 task_id（响应即终态），无法轮询 API-13；"
            "故直接把真实分段耗时随响应返回，完成后用它绘制时间轴。"
        ),
    )

    # ---- 软失败时的诊断信息（`API-12-2` / `FR-SYS-02`） ----
    error_code: Literal["E_TIMEOUT", "E_TOO_MANY_SERIES", "E_INFER_FAIL"] | None = None
    error_msg: str | None = None
    completed_stage: str | None = Field(
        None, description="软失败时已完成到的阶段（validate/preprocess/features/inference/metrics）"
    )
    suggestions: list[str] | None = Field(
        None, description="`UX-P07-05` 要求的三条处置建议"
    )


class TaskStatusResponse(AppBase):
    task_id: int
    status: Literal["created", "running", "succeeded", "failed"]
    stage: str | None = None
    stage_index: int = 0
    stage_total: int = 5
    progress_pct: float = 0.0
    device: Literal["cuda", "cpu"] | None = None
    device_fallback: int = 0
    elapsed_ms: int = 0
    budget_ms: int = SLA_SECONDS * 1000
    series_count: int = 0
    l1_count: int | None = None
    l2_count: int | None = None
    l3_count: int | None = None
    error_code: str | None = None
    error_msg: str | None = None
    completed_stage: str | None = None
