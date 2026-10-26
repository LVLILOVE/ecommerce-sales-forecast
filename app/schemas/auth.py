"""认证与用户（`API-01` ~ `API-05`）。

📌 `R-08`（2026-10-26）：模型真源已从 `code/backend/schemas.py` 迁到本包。
   `code/backend/schemas.py` 现在由 `scripts/gen_backend_schemas.py` **生成**，
   不要直接改那个文件（会被覆盖，且 `verify_ep9` 的 CHAIN 有同步守卫）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, EmailStr, Field, field_validator

from app.schemas.common import (
    PASSWORD_MAX_CHARS,
    TOKEN_VERSION_DEFAULT,
    AppBase,
    _enforce_bcrypt_bytes,
)

__all__ = [
    "RegisterRequest",
    "RegisterResponse",
    "JwtPayload",
    "LoginRequest",
    "UserProfile",
    "LoginResponse",
    "UpdateUserRequest",
]

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
