"""
=============================================================================
电商销量预测系统 · 后端请求/响应模型（Pydantic v2）
-----------------------------------------------------------------------------
⚠️ **本文件由 `scripts/gen_backend_schemas.py` 自动生成，请勿手工编辑。**
  修改会被下次生成覆盖，且 `python scripts/gen_backend_schemas.py --check`
  （已进 `verify_ep9` 的 CHAIN）会判定不一致而失败。

真源     : `app/schemas/`（common / auth / dataset / task / result / history / admin）
版本     : V1.1（`R-08`，2026-10-26）
依据文档 : 《开发技术文档 V1.0》第 4 章（后端设计）、第 5 章（接口设计）
对应前端 : code/frontend/types.ts（字段逐一对应，全链路 snake_case）

存在的理由
-----------------------------------------------------------------------------
`app/schemas/` 是运行期真源；本文件是**给静态使用方看的独立副本**：
  · `scripts/verify_schemas.py`        直接 import 它跑 54 条格式约束断言
  · `scripts/verify_ep6/7/8*.py`       读文本做正则 / AST 扫描
  · `scripts/verify_ep9_contract.py`   与前端 `types.ts` 做字段双向比对
这些脚本只把 `code/backend` 加进 `sys.path`，所以本文件必须能**单文件 import**，
不能改成一个转发 shim（那会 `ImportError`）。

设计约定（与真源一致）：
  1. 每个模型只负责「格式校验」，不承载业务规则（业务规则属 services 层）。
     例外：password 的强度规则由 PRD 明确规定（FR-USR-02），属格式约束。
  2. 响应模型使用 from_attributes = True，由 ORM 实体直接转换。
  3. 面向用户的 message 必须包含「错在哪 + 怎么改」（NFR-USE-03）。
=============================================================================
"""
from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# ---- 源模块：app/schemas/common.py ----------------------------------------

T = TypeVar("T")


class AppBase(BaseModel):
    """全项目模型基类。

    protected_namespaces=() 用于解除 pydantic 对 `model_` 前缀的保护：
    本项目的 model_version / model_loaded 是业务字段（模型版本、模型是否加载），
    与 pydantic 自身的 model_* 方法无关，不解除会持续产生告警。
    """

    model_config = ConfigDict(protected_namespaces=())
# =============================================================================
# 0. 常量（与 app/algo/config.py、前端 LIMITS 三处保持同源）
# =============================================================================

MAX_FILE_SIZE_MB = 20
MAX_ROWS = 500_000
MAX_SERIES = 1000
L1_MIN_VALID_POINTS = 90
ALLOWED_HORIZONS = (7, 14, 30)
REQUIRED_COLUMNS = ("sku_id", "warehouse_id", "date", "sales")
SLA_SECONDS = 60

# JWT 版本号初始值（TQ-1 决议）：与 users.token_version 列的 DEFAULT 一致。
# 载荷中的 ver 与该列不符即视为 Token 已失效；改密时该列 +1（FR-USR-07）。
TOKEN_VERSION_DEFAULT = 1

# bcrypt 5.x 对超过 72 字节的密码**直接抛 ValueError**（4.x 是静默截断，行为已变）。
# 且 72 是「字节」而非「字符」——64 个中文字符 = 192 字节，若只按字符限制，
# 注册接口会在 hashpw 处 500。故这里同时做字符与字节两道限制（BE-40 / JT-12）。
PASSWORD_MAX_CHARS = 64
BCRYPT_MAX_BYTES = 72


def _enforce_bcrypt_bytes(v: str) -> str:
    """按 UTF-8 字节数校验密码上限，与 bcrypt 的实际约束对齐。

    必须在 hash_password 之前拦截，否则 ValueError 会冒泡成 500，
    而不是返回 4xx 的可读提示（NFR-USE-03）。
    """
    n = len(v.encode("utf-8"))
    if n > BCRYPT_MAX_BYTES:
        raise ValueError(
            f"密码编码后不得超过 {BCRYPT_MAX_BYTES} 字节（当前 {n} 字节）。"
            "中文等字符每个占 3 字节，请缩短密码或改用数字与字母"
        )
    return v
# =============================================================================
# 1. 通用响应包装（FR-SYS-01：所有接口结构一致）
# =============================================================================


class ApiResponse(AppBase, Generic[T]):
    """统一响应包。

    成功：{"code": 0, "message": "ok", "data": {...}}
    失败：{"code": 40001, "message": "...", "data": {"定位信息": ...}}
    """

    code: int = 0
    message: str = "ok"
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(cls, code: int, message: str, data: Any = None) -> "ApiResponse[T]":
        return cls(code=code, message=message, data=data)


class Paginated(AppBase, Generic[T]):
    items: list[T]
    page: int
    size: int
    total: int
    pages: int

    @classmethod
    def build(cls, items: list[T], page: int, size: int, total: int) -> "Paginated[T]":
        pages = (total + size - 1) // size if size else 0
        return cls(items=items, page=page, size=size, total=total, pages=pages)


class ValidationErrorData(AppBase):
    """校验失败的定位载荷 —— 必须能直接渲染成可读提示（FR-UPL-05）。"""

    fail_stage: str = Field(..., description="失败所在校验步骤")
    missing_columns: list[str] | None = None
    unparsable_rows: list[dict[str, Any]] | None = None
    negative_rows: list[dict[str, Any]] | None = None
    gap_ranges: list[dict[str, Any]] | None = None
    series_count: int | None = None
    series_limit: int | None = None
    # EP-4 契约补充（裁定 A「失败也建记录」）：校验失败时数据集记录**已创建**，
    # 前端据此调用 `API-11`（错误明细下载）与 `POST /datasets/gap`（断档处置）。
    # ⚠️ 未完整落盘的两种情形（扩展名非 .csv、文件大小超限）不建记录，
    #    此时本字段为 None —— 前端必须判空，不得假定它一定存在。
    dataset_id: int | None = None
    suggestion: str = Field(..., description="修复建议，禁止留空（NFR-USE-03）")
# =============================================================================
# 7. 系统                                              API-20
# =============================================================================


class HealthResponse(AppBase):
    status: Literal["ok", "degraded"] = "ok"
    version: str
    database: Literal["ok", "error"] = "ok"
    model_loaded: bool
    model_version: str | None = None
    device: Literal["cuda", "cpu"] | None = None
    uptime_seconds: int = 0


# ---- 源模块：app/schemas/auth.py ----------------------------------------

# =============================================================================
# 2. 认证与用户                                        API-01 ~ API-05
# =============================================================================


class RegisterRequest(AppBase):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=PASSWORD_MAX_CHARS)
    nickname: str | None = Field(None, max_length=32)

    @field_validator("password")
    @classmethod
    def check_strength(cls, v: str) -> str:
        """FR-USR-02：长度 >= 8，且至少含字母与数字两类字符。"""
        has_alpha = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_alpha and has_digit):
            raise ValueError("密码需不少于 8 位，且同时包含字母与数字")
        return _enforce_bcrypt_bytes(v)


class RegisterResponse(AppBase):
    user_id: int
    email: str


class JwtPayload(AppBase):
    """JWT 载荷结构（BE-19 / TQ-1 决议）。

    该结构不出现在接口响应中（Token 是不透明串），但它是后端签发与校验、
    以及运维排障时解 Token 的共同契约，故在此显式声明，避免实现时字段名漂移。

    ver 与 users.token_version 的比对是 FR-USR-07（改密后旧 Token 立即失效）
    的唯一落地点：JWT 无状态，无法枚举某用户已签发的所有 Token。
    """

    sub: str = Field(..., description="用户 id 的字符串形式（JWT 规范要求 sub 为字符串）")
    ver: int = Field(TOKEN_VERSION_DEFAULT, description="签发时的 token_version 快照")
    jti: str = Field(..., description="Token 唯一标识，登出时写入内存黑名单")
    iat: int = Field(..., description="签发时间（Unix 秒）")
    exp: int = Field(..., description="过期时间（Unix 秒）")


class LoginRequest(AppBase):
    email: EmailStr
    password: str


class UserProfile(AppBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    nickname: str | None = None
    status: int = 1
    # CH-01：角色。前端据此派生 isAdmin（决定导航是否显示管理端入口）。
    # ⚠️ 这是「展示用」的自我描述，不是权限凭证——真正的越权判定在
    #    get_current_admin（服务端），前端不得仅凭本字段放行管理端操作。
    role: Literal["user", "admin"] = "user"
    last_login_at: str | None = None
    created_at: str


class LoginResponse(AppBase):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(..., description="有效期，单位秒")
    user: UserProfile


class UpdateUserRequest(AppBase):
    nickname: str | None = Field(None, max_length=32)
    old_password: str | None = None
    new_password: str | None = Field(None, min_length=8, max_length=PASSWORD_MAX_CHARS)

    @field_validator("new_password")
    @classmethod
    def check_strength(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
            raise ValueError("密码需不少于 8 位，且同时包含字母与数字")
        return _enforce_bcrypt_bytes(v)


# ---- 源模块：app/schemas/dataset.py ----------------------------------------

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


# ---- 源模块：app/schemas/task.py ----------------------------------------

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


# ---- 源模块：app/schemas/result.py ----------------------------------------

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


# ---- 源模块：app/schemas/history.py ----------------------------------------

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


# ---- 源模块：app/schemas/admin.py ----------------------------------------

# =============================================================================
# 8. 管理端（`CH-01`）                                API-22 ~ API-30
# =============================================================================
# 🔒 本节是数据隔离红线的**唯一显式例外**（`BE-ADM-01` / `NFR-SEC-02`）：
#    9 个接口全部跨用户聚合，例外范围**只允许** `app/api/v1/admin.py`。
#
# 🔒 **全节不得出现任何密码相关字段** —— 连「重置密码」的请求模型都只收
#    `new_password`，**没有任何字段返回密码哈希**（`NFR-SEC-05` 最小暴露）。
#    `verify_ep8.py` 会对本节做「password_hash 零命中」断言。


# ---- 查询参数 --------------------------------------------------------------
class AdminOverviewQuery(AppBase):
    """`API-22` 可选跨度。范围 `[1,180]`，非法值由服务层返 `40012`。"""

    days: int | None = 30


class AdminUserQuery(AppBase):
    """`API-23` 查询参数。

    ⚠️ `status` / `role` **刻意不在此收窄取值域**：pydantic 收窄会抛
    `ValidationError` → 500，而非法筛选值应按本项目错误契约返 `40012`，
    由服务层白名单把关（与 `EP-7` 的 `HistoryQuery.status` 同理）。
    """

    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    q: str | None = Field(None, max_length=64)
    status: int | None = None
    role: str | None = None


class AdminTaskQuery(AppBase):
    """`API-26` 查询参数（`status` 同上，不在schema 层收窄）。"""

    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    user_id: int | None = None
    status: str | None = None
    q: str | None = Field(None, max_length=64)


class AdminLogQuery(AppBase):
    """`API-30` 查询参数（`action` / `result` 同上，不在 schema 层收窄）。"""

    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    action: str | None = None
    result: str | None = None
    user_id: int | None = None
    start: str | None = None
    end: str | None = None


# ---- 请求体 ----------------------------------------------------------------
class AdminUpdateUserRequest(AppBase):
    """`API-24` 启停用 + 角色变更。两项都可选，但**至少要传一项**。

    🔒「至少一项」由服务层校验（返 `40012`）而不是用 pydantic 的
       `model_validator` —— 因为本项目约定「业务规则属 services 层」，
       而「两个可选项不能同时为空」本质是业务规则。

    ⚠️ **类名必须带 `Admin` 前缀**：本模块上方另有一个 `UpdateUserRequest`
    （`API-05` 用户改自己的昵称/密码，字段 `nickname`/`old_password`/`new_password`）。
    两者字段完全不同。若此处沿用同名 —— Python 会**静默用后定义者覆盖先定义者**，
    后果是 `API-05` 读到的是本类（无 `nickname` 属性），一调用就 `AttributeError` → 500。
    📌 这是 `EP-9` 全量回归 `verify_schemas.py` 时抓到的真实缺陷；已加重名守卫。
    """

    status: Literal[0, 1] | None = None
    role: Literal["user", "admin"] | None = None


class ResetPasswordRequest(AppBase):
    """`API-25` 重置密码。

    🔒 强度规则与 `RegisterRequest` **完全同源**（≥8 位、含字母与数字、
       ≤72 字节）：管理员重置出来的密码必须满足与用户自己注册一样的门槛，
       否则会出现「管理员设的密码比用户能设的还弱」。
    """

    new_password: str = Field(..., min_length=8, max_length=PASSWORD_MAX_CHARS)

    @field_validator("new_password")
    @classmethod
    def check_strength(cls, v: str) -> str:
        """与 `RegisterRequest.check_strength` 逐字同源，改一处必须改两处。"""
        has_alpha = any(c.isalpha() for c in v)
        has_digit = any(c.isdigit() for c in v)
        if not (has_alpha and has_digit):
            raise ValueError("密码需不少于 8 位，且同时包含字母与数字")
        return _enforce_bcrypt_bytes(v)


class SwitchModelRequest(AppBase):
    """`API-29` 切换启用版本。"""

    is_active: bool = True


# ---- 响应模型 --------------------------------------------------------------
class MapeTrendPoint(AppBase):
    """`API-22` 折线点。`weighted_mape` 可空（任务未成功 → 无 metrics）。"""

    task_id: int
    task_name: str
    created_at: str
    weighted_mape: float | None = None
    status: str


class ActiveModelBrief(AppBase):
    """`API-22` 当前启用模型摘要。

    🔒 `env_match` 是 `BE-ADM-03` 的可视化落点：`.env` 的 `ACTIVE_MODEL`
       与库不一致时，看板必须**显眼地**告诉运营「需要改 `.env` 并重启」，
       否则会出现「数据库说 v2、进程跑v1」的静默故障。
    """

    version: str
    window_size: int
    hidden_size: int
    num_layers: int
    param_count: int | None = None
    val_mape: float | None = None
    trained_at: str | None = None
    device: str | None = None
    is_active: bool = True
    env_match: bool = True


class AdminOverviewResponse(AppBase):
    """`API-22` 运营看板概览。"""

    user_count: int
    admin_count: int
    dataset_count: int
    task_count: int
    success_rate: float | None = None
    avg_duration_ms: float | None = None
    avg_mape: float | None = None
    mape_trend: list[MapeTrendPoint] = Field(default_factory=list)
    active_model: ActiveModelBrief | None = None
    event_counts: dict[str, int] = Field(default_factory=dict)
    days: int = 30


class AdminUserBrief(AppBase):
    """`API-23` 用户摘要。

    🔒 **无 `password_hash` 字段且永不可加**（`NFR-SEC-05`）——
       这是本项目「最小暴露」的第一条红线，`verify_ep8.py` 对此断言。
    """

    id: int
    email: str
    nickname: str | None = None
    status: int
    role: str
    last_login_at: str | None = None
    created_at: str
    updated_at: str | None = None


class AdminUserPage(AppBase):
    items: list[AdminUserBrief] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 20


class AdminUpdateUserResponse(AppBase):
    """`API-24` 更新后的用户摘要。"""

    user: AdminUserBrief


class ResetPasswordResponse(AppBase):
    """`API-25` 重置结果。

    🔒 返回 `token_version`（已 +1）是刻意的：让管理员能确认
       「旧登录态确实全部失效了」，而不必自己去查库。
    """

    user_id: int
    email: str
    token_version: int
    message: str


class AdminTaskBrief(AppBase):
    """`API-26` 任务摘要（跨用户）。

    ⚠️ `is_deleted=1` 的任务**也在列表里**并带该标记：管理端是运维视角，
       需要看清用户删过什么（用户侧列表才过滤）。
    """

    id: int
    task_name: str
    user_id: int
    owner_email: str
    status: str
    is_deleted: int = 0
    granularity: str
    horizon: int
    model_version: str | None = None
    weighted_mape: float | None = None
    duration_ms: int | None = None
    created_at: str


class AdminTaskPage(AppBase):
    items: list[AdminTaskBrief] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 20


class AdminDeleteTaskResponse(AppBase):
    """`API-27` 强制删除回执。

    🔒 返回**连带删除的行数**是产品要求，不是顺手加的：不可逆操作
       没有回执，管理员不敢点。
    """

    task_id: int
    deleted_predictions: int
    deleted_metrics: int
    message: str


class AdminModelBrief(AppBase):
    """`API-28` 模型版本摘要。"""

    id: int
    version: str
    window_size: int
    hidden_size: int
    num_layers: int
    dropout: float
    learning_rate: float
    batch_size: int
    param_count: int | None = None
    val_mape: float | None = None
    val_mae: float | None = None
    val_rmse: float | None = None
    device: str | None = None
    peak_memory_mb: float | None = None
    train_data_range: str | None = None
    trained_at: str | None = None
    created_at: str
    is_active: bool = False
    env_match: bool = True


class AdminModelListResponse(AppBase):
    items: list[AdminModelBrief] = Field(default_factory=list)
    active_version: str
    total: int = 0


class SwitchModelResponse(AppBase):
    """`API-29` 切换结果。

    🔒 `warnings` **不可省**：`BE-ADM-03` 要求切换后校验与 `.env` 一致，
       不一致时接口仍返回成功（数据确实切了），但必须把「需要改 .env
       并重启」显式告诉前端 —— 否则就是静默故障。
    """

    version: str
    is_active: bool = True
    changed: bool = False
    previous_version: str | None = None
    env_match: bool = True
    reloaded: bool = False
    warnings: list[str] = Field(default_factory=list)
    message: str


class AdminLogBrief(AppBase):
    """`API-30` 日志条目。

    🔒 `sub_action` 是**结构化拆好**的子动作（`detail` 里的 `sub=`），
       不是让前端自己 `split(';')` —— 格式由后端保证，前端只管渲染。
    """

    id: int
    user_id: int | None = None
    action: str
    target_id: str | None = None
    result: str
    detail: str | None = None
    sub_action: str | None = None
    operator_id: int | None = None
    ip: str | None = None
    created_at: str


class AdminLogPage(AppBase):
    items: list[AdminLogBrief] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 20
