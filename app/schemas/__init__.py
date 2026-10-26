"""
=============================================================================
请求 / 响应模型（Pydantic v2）—— **模型真源**
-----------------------------------------------------------------------------
版本     : V1.1（`R-08`，2026-10-26 反向依赖修正）
依据文档 : 《开发技术文档 V1.0》§2.2（目录结构）、§4.2（接口子层职责）
           《项目开发实施方案 V1.0》§2.2
-----------------------------------------------------------------------------
📌 按业务域拆成 7 个模块（`R-08`）：
    common   模型基类 / 业务常量 / 统一响应包装 / 健康检查
    auth     认证与用户           API-01 ~ 05
    dataset  数据集               API-06 ~ 11
    task     预测任务             API-12 ~ 15
    result   结果与明细           API-14
    history  历史与对比           API-16 ~ 19
    admin    管理端               API-22 ~ 30

🔴 **依赖方向已反转**（`R-08` 的核心）
    历史做法：`app/schemas/__init__.py` 用 `spec_from_file_location` 在**运行期**
    按绝对路径加载包外的 `code/backend/schemas.py`，再由 `verify_schemas.py`
    直接 import 那个文件。好处是「只有一份定义」，代价是三条：
      ① 打包/`pip install` 后找不到 `code/`，应用直接起不来；
      ② 静态分析与 IDE 跳转全部失效（模型不在任何包路径上）；
      ③ 部署必须把整个 `code/` 目录当作运行期依赖（踩过一次 `ImportError`）。

    现在：**本包是唯一真源**；`code/backend/schemas.py` 由
    `scripts/gen_backend_schemas.py` **生成**（把 7 个模块按依赖顺序拼成一个
    独立文件），供文档锚点、`verify_schemas.py` 与既有 5 个 `verify_*` 脚本的
    文本/AST 扫描继续使用。
    ⇒ 改模型只改 `app/schemas/*.py`，然后跑 `python scripts/gen_backend_schemas.py`；
      忘了生成会被 `scripts/gen_backend_schemas.py --check` 拦下（已进 `verify_ep9` 的 CHAIN）。

⚠️ 生成顺序即依赖顺序：`common → auth → dataset → task → result → history → admin`。
   改变这个顺序会破坏拼接后文件里的前向引用（pydantic 在类创建时就要解析注解）。
=============================================================================
"""
from __future__ import annotations

from app.schemas.common import (
    AppBase,
    ApiResponse,
    Paginated,
    ValidationErrorData,
    HealthResponse,
    T,
    MAX_FILE_SIZE_MB,
    MAX_ROWS,
    MAX_SERIES,
    L1_MIN_VALID_POINTS,
    ALLOWED_HORIZONS,
    REQUIRED_COLUMNS,
    SLA_SECONDS,
    TOKEN_VERSION_DEFAULT,
    PASSWORD_MAX_CHARS,
    BCRYPT_MAX_BYTES,
    _enforce_bcrypt_bytes,
)
from app.schemas.auth import (
    RegisterRequest,
    RegisterResponse,
    JwtPayload,
    LoginRequest,
    UserProfile,
    LoginResponse,
    UpdateUserRequest,
)
from app.schemas.dataset import (
    DatasetSummary,
    DatasetPreview,
    GapWarning,
    UploadDatasetResponse,
    ResolveGapRequest,
    LevelPreview,
    HorizonEstimate,
    DatasetDetailResponse,
)
from app.schemas.task import (
    CreateTaskRequest,
    CreateTaskResponse,
    TaskStatusResponse,
)
from app.schemas.result import (
    MetricBundle,
    Coverage,
    Distribution,
    TimingMs,
    SeriesCurve,
    PredictionRow,
    TaskBrief,
    DatasetBrief,
    PredictionResultResponse,
    DetailQuery,
    ExportQuery,
    DETAIL_SORT_COLUMNS,
    DETAIL_LEVELS,
)
from app.schemas.history import (
    HistoryQuery,
    HistoryItem,
    HistoryPage,
    CompareRequest,
    CompareSeries,
    CompareTaskSlot,
    CompareResponse,
    WorkbenchSummary,
)
from app.schemas.admin import (
    AdminOverviewQuery,
    AdminUserQuery,
    AdminTaskQuery,
    AdminLogQuery,
    AdminUpdateUserRequest,
    ResetPasswordRequest,
    SwitchModelRequest,
    MapeTrendPoint,
    ActiveModelBrief,
    AdminOverviewResponse,
    AdminUserBrief,
    AdminUserPage,
    AdminUpdateUserResponse,
    ResetPasswordResponse,
    AdminTaskBrief,
    AdminTaskPage,
    AdminDeleteTaskResponse,
    AdminModelBrief,
    AdminModelListResponse,
    SwitchModelResponse,
    AdminLogBrief,
    AdminLogPage,
)

__all__ = [
    "AppBase",
    "ApiResponse",
    "Paginated",
    "ValidationErrorData",
    "HealthResponse",
    "T",
    "MAX_FILE_SIZE_MB",
    "MAX_ROWS",
    "MAX_SERIES",
    "L1_MIN_VALID_POINTS",
    "ALLOWED_HORIZONS",
    "REQUIRED_COLUMNS",
    "SLA_SECONDS",
    "TOKEN_VERSION_DEFAULT",
    "PASSWORD_MAX_CHARS",
    "BCRYPT_MAX_BYTES",
    "_enforce_bcrypt_bytes",
    "RegisterRequest",
    "RegisterResponse",
    "JwtPayload",
    "LoginRequest",
    "UserProfile",
    "LoginResponse",
    "UpdateUserRequest",
    "DatasetSummary",
    "DatasetPreview",
    "GapWarning",
    "UploadDatasetResponse",
    "ResolveGapRequest",
    "LevelPreview",
    "HorizonEstimate",
    "DatasetDetailResponse",
    "CreateTaskRequest",
    "CreateTaskResponse",
    "TaskStatusResponse",
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
    "HistoryQuery",
    "HistoryItem",
    "HistoryPage",
    "CompareRequest",
    "CompareSeries",
    "CompareTaskSlot",
    "CompareResponse",
    "WorkbenchSummary",
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
