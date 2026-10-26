"""`revoked_tokens` Token 黑名单表 ORM 实体（`DB-T09`）。

对应 `code/db/schema.sql` 的 `T09` 段（`R-03`，2026-10-26）。

📌 这张表是「登出」这件事的**唯一真源**。此前 `jti` 黑名单放在
   `app/core/security.py` 的进程内存字典里，有两个静默失效的场景：
     ① 进程重启 → 已登出的 Token 又恢复可用（直到 `exp` 自然过期）；
     ② 多 worker → A 进程登出，旧 Token 打到 B 进程照样放行。
   两者都不报错，只是让「登出」不可靠 —— 属于最难被发现的一类缺陷。

📌 `jti` 是主键 ⇒ 重复登出天然幂等（DAO 用 `INSERT OR IGNORE`），
   不需要业务层先查后插（那样在并发下会撞唯一索引）。

🔒 `expires_at` 存 **Unix 秒**而不是 TEXT：它要与 JWT 的 `exp` 直接比较，
   存文本反而要来回解析；且这里的「时间」是机器时间，不参与本地时区展示
   （`NFR-COMP-04` 的单时区假设针对的是给人看的时间字段）。
"""
from __future__ import annotations

from sqlalchemy import INTEGER, TEXT, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"
    __table_args__ = (Index("ix_revoked_tokens_exp", "expires_at"),)

    jti: Mapped[str] = mapped_column(TEXT, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(INTEGER, nullable=True, default=None)
    expires_at: Mapped[int] = mapped_column(INTEGER, nullable=False)
    created_at: Mapped[str] = mapped_column(
        TEXT, nullable=False, server_default=text("(datetime('now', 'localtime'))")
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RevokedToken jti={self.jti!r} exp={self.expires_at}>"
