"""`revoked_tokens` 表数据访问（`DB-T09`）。

🔒 纯 CRUD。这是「登出」这件事的**唯一真源**（`R-03`，2026-10-26）。
   历史背景：早期把 `jti` 黑名单放在 `app/core/security.py` 的进程内存字典里，
   有两个**静默失效**的场景 —— 进程重启后已登出的 Token 又恢复可用；
   多 worker 时 A 进程登出、旧 Token 打 B 进程照样放行。两者都不报错，
   只是让「登出」变得不可靠，属于最难被发现的一类缺陷。

🔒 本模块不得 `import algo`，也不得 `import fastapi`（分层单向）。
"""
from __future__ import annotations

import time

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.revoked_token import RevokedToken

__all__ = ["revoke", "is_revoked", "purge_expired", "count"]


def revoke(
    db: Session,
    *,
    jti: str,
    expires_at: int,
    user_id: int | None = None,
) -> None:
    """把某个 Token 的 `jti` 拉黑（登出，`FR-USR-05`）。

    🔒 **用 `INSERT ... ON CONFLICT DO NOTHING`，不做「先查后插」**：
       同一个 Token 连点两次「登出」在真实网络里很常见（超时重试 / 双击），
       先查后插在并发下会撞主键并冒泡成 500 —— 一个本该幂等的操作变成报错。

    :param expires_at: Token 的原 `exp`（Unix 秒）。到期后本行会被清理，
                       因此表规模上限 = 「同时段内登出的 Token 数」。
    """
    stmt = (
        sqlite_insert(RevokedToken)
        .values(jti=jti, user_id=user_id, expires_at=int(expires_at))
        .on_conflict_do_nothing(index_elements=["jti"])
    )
    db.execute(stmt)
    db.flush()


def is_revoked(db: Session, jti: str) -> bool:
    """该 `jti` 是否已被登出拉黑。

    ⚠️ 每条鉴权请求都会查一次。`jti` 是主键，SQLite 走的是 B-tree 主键查找，
       代价与读取 `users.token_version` 同量级 —— 这也是「直接查库、
       不搞进程内缓存」的前提：引入缓存就得处理多 worker 下的一致性，
       而在当前规模下省下的那点时间不值得那个复杂度。
    """
    return (
        db.execute(select(RevokedToken.jti).where(RevokedToken.jti == jti)).first()
        is not None
    )


def purge_expired(db: Session, *, now: int | None = None) -> int:
    """删除已自然过期的黑名单行，返回删除条数（启动时调用一次即可）。"""
    now = int(time.time()) if now is None else int(now)
    res = db.execute(delete(RevokedToken).where(RevokedToken.expires_at <= now))
    db.flush()
    return int(res.rowcount or 0)


def count(db: Session, *, alive_only: bool = True) -> int:
    """当前黑名单条目数（自检与排障用）。"""
    stmt = select(func.count()).select_from(RevokedToken)
    if alive_only:
        stmt = stmt.where(RevokedToken.expires_at > int(time.time()))
    return int(db.execute(stmt).scalar_one())
