"""
=============================================================================
安全原语（密码哈希 / JWT 签发与校验 / Token 黑名单）
-----------------------------------------------------------------------------
版本     : V1.0（EP-2）
依据文档 : 《开发技术文档 V1.0》§4.5（BE-19 Token 方案）、§5.4（认证接口）
           《PRD V1.0》FR-USR-02 / 03 / 04 / 05 / 07
           《项目开发实施方案 V1.0》WS-2-01、RG-06、JT-12
-----------------------------------------------------------------------------
三条必须保持的设计铁律
-----------------------------------------------------------------------------
1. 🔒 **密码上限是「字节」不是「字符」**（`JT-12` / `RG-06`）
   bcrypt 5.x 对 >72 **字节** 的输入直接抛 `ValueError`（4.x 是静默截断到 72，
   行为已变）。64 个中文字符 = 192 字节，若只按字符数限制，注册接口会在
   `hashpw` 处冒泡成 500 而不是可控的 4xx。故本模块是**最后一道防线**：
   `hash_password` / `verify_password` 都必须在触碰 bcrypt 之前完成字节校验。

2. 🔒 **`hash_password` 与 `seed.sql` 的 cost 必须一致**（当前 12）
   `code/db/seed.sql` 的管理员种子串由 `gensalt(rounds=12)` 生成。
   若运行时用默认 cost（12，恰好相同）以外值，种子账号仍能登录（bcrypt 校验
   读哈希自带 cost），但**新注册用户与种子的强度不一致**，运维核对时会误判。
   故这里显式写死 `BCRYPT_ROUNDS = 12`。

3. 🔒 **改密后旧 Token 必须立即失效**（`FR-USR-07`）
   JWT 无状态，无法枚举某用户已签发的全部 Token，所以「按 sub 拉黑」不可行。
   本项目采用**双机制**：
     - `token_version`（落在 `users` 表，`TQ-1` 方案 C）：改密时 +1，
       使该用户**全部**历史 Token 的 `ver` 与库不符 → 全部失效（主机制）；
     - `jti` 黑名单（`revoked_tokens` 表，`DB-T09`）：登出时把单个 Token 的
       `jti` 拉黑，使**当前这一个** Token 立即失效（`FR-USR-05`）。
   二者不可互相替代：黑名单管「单个 Token 主动登出」，版本号管「一次改密全撤」。

⚠️ 黑名单**不在本模块实现**（`R-03`，2026-10-26 已迁库）。
   本文件只做无状态的密码 / JWT 原语；「这个 jti 是否已撤销」由
   `deps.get_auth_context()` 用 `token_dao.is_revoked()` 判定。
   历史教训：内存版在多 worker / 重启后会静默失效，不要改回去。
=============================================================================
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core import config
from app.core.errors import BizError, ErrorCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
BCRYPT_ROUNDS = 12
"""bcrypt cost 因子。🔒 必须与 `code/db/seed.sql` 的管理员种子串一致。"""

BCRYPT_MAX_BYTES = 72
"""bcrypt 的硬上限（字节）。与 `app/schemas/common.py::BCRYPT_MAX_BYTES` 同源。"""

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "sales-forecast-api"
"""签发者标识，写进 `iss` 声明并在校验时比对，避免与其他系统共用密钥时串号。"""

# ---------------------------------------------------------------------------
# ⚠️ Token 黑名单**不在本模块**
# ---------------------------------------------------------------------------
# `R-03`（2026-10-26）：黑名单已从「进程内存字典」迁到数据库表
# `revoked_tokens`（见 `app/models/revoked_token.py` + `app/dao/token_dao.py`）。
#
# 🔒 **不要再往本模块加回内存实现。** 内存版有两个静默失效的场景：
#   ① 进程重启 → 已登出的 Token 又恢复可用（直到 exp 自然过期）；
#   ② 多 worker → A 进程登出，旧 Token 打 B 进程照样放行。
# 二者都不报错，只是让「登出」不可靠。
#
# 分层上也更干净了：本模块只做「密码哈希 + JWT 编解码」这类**无状态**原语，
# 「这个 jti 是否已撤销」是**持久化状态**，归属 `deps.get_auth_context()` 判定
# （见那里的注释）。


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def _assert_within_bcrypt_limit(plain: str) -> bytes:
    """把明文编码为 UTF-8 并校验 72 字节上限。

    ⚠️ 这里必须**在** `bcrypt.hashpw` 之前调用：bcrypt 5.x 超限是抛
       `ValueError`，冒泡到路由层会变成 500 而非可读的 4xx（`NFR-USE-03`）。
    """
    raw = plain.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise BizError(
            ErrorCode.WEAK_PASSWORD,
            f"密码编码后不得超过 {BCRYPT_MAX_BYTES} 字节（当前 {len(raw)} 字节）。"
            "中文等字符每个占 3 字节，请缩短密码或改用数字与字母",
            {"max_bytes": BCRYPT_MAX_BYTES, "actual_bytes": len(raw)},
        )
    return raw


def hash_password(plain: str) -> str:
    """bcrypt 哈希（含随机 salt）。返回 60 字符的 `$2b$12$...` 串。

    🔒 数据库里**只能**存本函数的返回值，任何情况下不得存明文（`FR-USR-03`）。
    """
    raw = _assert_within_bcrypt_limit(plain)
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与哈希是否匹配。

    返回 `False`（而不是抛错）的两种情况，都是**刻意的**：

    1. 输入 > 72 字节：本项目所有账号的密码在注册时就被限制在 72 字节内，
       故超长输入**不可能**匹配任何存量哈希。直接判否，既避免 bcrypt 抛
       `ValueError` 变成 500，也不给攻击者任何额外信息。
    2. `hashed` 不是合法 bcrypt 串（如游客哨兵 `id=0` 的占位值 `'!'`）：
       捕获 `ValueError` 后判否。哨兵账号因此天然无法登录——这是
       `JT-10` 的**防登录设计**，不是缺陷，不要「修」它。

    ⚠️ `checkpw` 是**恒时**的吗？不是严格恒时，但 bcrypt 的代价主要在
       key schedule 上，文本比对差异极小，且本项目登录已有限流兜底。
    """
    if len(plain.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # 非法哈希（哨兵占位值 `'!'`、列被写坏等）——判否，不抛
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def _secret() -> str:
    """取密钥。缺配置时抛错，而不是静默用空串签名（那等于没有签名）。"""
    secret = config.settings.jwt_secret
    if not secret:
        raise RuntimeError(
            "JWT_SECRET 未配置，无法签发或校验 Token（BE-05）。"
            "请检查项目根目录的 .env。"
        )
    return secret


def create_access_token(user_id: int, token_version: int) -> tuple[str, int]:
    """签发访问 Token。

    :return: `(token, expires_in_seconds)`

    载荷字段与 `app/schemas/auth.py::JwtPayload` 及前端 `types.ts` 三处同源：
        `sub` 用户 id 的**字符串**形式（JWT 规范要求 sub 为字符串）
        `ver` 签发瞬间的 `users.token_version` 快照（改密后立即失效的依据）
        `jti` Token 唯一标识（登出时拉黑的键）
        `iat` / `exp` 签发与过期时间（Unix 秒）
    """
    expires_in = int(config.settings.jwt_expire_minutes) * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "ver": int(token_version),
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "iss": JWT_ISSUER,
    }
    token = jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> dict:
    """校验并解出 Token 载荷（**只做无状态校验**）。

    🔒 所有失败路径统一抛 `40102 TOKEN_EXPIRED`（真实 HTTP 401），**不区分**
       「签名错 / 过期 / 格式坏」——区分开来等于给攻击者提供调试信息。

    🔒 本函数**不检查** `jti` 是否已登出（`R-03` 之后）。原因：
       黑名单在数据库里，而本模块按设计不持有 `Session`。
       登出判定的位置是 `deps.get_auth_context()` / `deps.get_optional_user_id()`，
       它们手上有 `db`，在拿到 payload 之后紧接着查 `token_dao.is_revoked()`。
       ⇒ 任何**绕过 deps 直接调用本函数**的新代码，都不会自动获得登出保护。
       这是刻意的分层代价：无状态原语不做持久化查询，避免 security 依赖 SQLAlchemy。
    """

    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["sub", "exp", "iat", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        logger.info("token rejected: expired")
        raise BizError(ErrorCode.TOKEN_EXPIRED, "登录已过期，请重新登录") from None
    except jwt.InvalidTokenError as exc:
        logger.info("token rejected: invalid (%s)", type(exc).__name__)
        raise BizError(ErrorCode.TOKEN_EXPIRED, "登录状态无效，请重新登录") from None

    return payload


def payload_user_id(payload: dict) -> int:
    """从载荷取用户 id。`sub` 是字符串，转 int 失败按 Token 失效处理。"""
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise BizError(ErrorCode.TOKEN_EXPIRED, "登录状态无效，请重新登录") from None


def payload_token_version(payload: dict) -> int:
    """从载荷取签发时的 `token_version`。缺失按 0 处理（必然与库中 ≥1 不符）。"""
    try:
        return int(payload.get("ver", 0))
    except (TypeError, ValueError):
        return 0
