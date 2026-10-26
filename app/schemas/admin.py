"""管理端（`API-22` ~ `API-30`）—— 🔒 数据隔离红线的唯一显式例外。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import PASSWORD_MAX_CHARS, AppBase, _enforce_bcrypt_bytes

__all__ = [
    "AdminOverviewQuery",
    "AdminUserQuery",
    "AdminTaskQuery",
    "AdminLogQuery",
    "AdminUpdateUserRequest",
    "ResetPasswordRequest",
    "SwitchModelRequest",
    "MapeTrendPoint",
    "ActiveModelBrief",
    "AdminOverviewResponse",
    "AdminUserBrief",
    "AdminUserPage",
    "AdminUpdateUserResponse",
    "ResetPasswordResponse",
    "AdminTaskBrief",
    "AdminTaskPage",
    "AdminDeleteTaskResponse",
    "AdminModelBrief",
    "AdminModelListResponse",
    "SwitchModelResponse",
    "AdminLogBrief",
    "AdminLogPage",
]

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
