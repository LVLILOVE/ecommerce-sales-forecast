"""通用件：模型基类、业务常量、统一响应包装、健康检查。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

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
]

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
