"""Token 黑名单（`revoked_tokens`，`DB-T09` / `R-03`）的行为测试。

这一套是**真库测试**（SQLite 临时文件），但仍然毫秒级：只建表、不开服务、
不加载模型。之所以不用 mock，是因为这里要验证的恰恰是「落到了库里」——
mock 掉 Session 就等于什么都没测。

历史背景（写成测试的原因）：黑名单早期放在 `app/core/security.py` 的进程内存
字典里，有两个**静默失效**的场景 —— 进程重启后已登出的 Token 又恢复可用；
多 worker 时 A 进程登出、旧 Token 打 B 进程照样放行。上面两条都不报错，
所以用单测把「不落内存」这件事钉住，比写在注释里更有效。
"""
from __future__ import annotations

import time

import pytest

from app.core import security
from app.core.db import SessionLocal, init_db
from app.dao import token_dao


@pytest.fixture(scope="module")
def db():
    """建一份临时库（conftest 已把 DB_PATH 指向临时目录），产出长生命周期会话。"""
    init_db(run_seed=False)
    with SessionLocal() as session:
        yield session


class TestRevoke:
    def test_revoke_then_is_revoked(self, db) -> None:
        jti = "t" + str(int(time.time() * 1000))
        assert token_dao.is_revoked(db, jti) is False
        token_dao.revoke(db, jti=jti, expires_at=int(time.time()) + 3600, user_id=1)
        db.commit()
        assert token_dao.is_revoked(db, jti) is True

    def test_revoke_is_idempotent(self, db) -> None:
        """连点两次「登出」在真实网络里很常见（超时重试 / 双击）。

        旧实现用「先查后插」，并发下会撞主键冒泡成 500 —— 一个本该幂等的
        操作变成报错。现在走 `INSERT ... ON CONFLICT DO NOTHING`。
        """
        jti = "dup" + str(int(time.time() * 1000))
        token_dao.revoke(db, jti=jti, expires_at=int(time.time()) + 60)
        token_dao.revoke(db, jti=jti, expires_at=int(time.time()) + 60)  # 不应抛
        db.commit()
        assert token_dao.is_revoked(db, jti) is True


class TestPurge:
    def test_purge_removes_only_expired(self, db) -> None:
        now = int(time.time())
        dead = "dead" + str(now)
        alive = "alive" + str(now)
        token_dao.revoke(db, jti=dead, expires_at=now - 1)
        token_dao.revoke(db, jti=alive, expires_at=now + 3600)
        db.commit()

        removed = token_dao.purge_expired(db, now=now)
        db.commit()

        assert removed >= 1
        assert token_dao.is_revoked(db, dead) is False, "过期行必须被清掉"
        assert token_dao.is_revoked(db, alive) is True, "未过期行不能被误删"

    def test_count_counts_only_alive(self, db) -> None:
        now = int(time.time())
        token_dao.revoke(db, jti="c_dead" + str(now), expires_at=now - 10)
        token_dao.revoke(db, jti="c_live" + str(now), expires_at=now + 600)
        db.commit()
        alive = token_dao.count(db)
        total = token_dao.count(db, alive_only=False)
        assert total > alive, f"total={total} 应大于 alive={alive}"


class TestNoMemoryFallback:
    """🔒 守卫：「别把内存实现加回来」。

    这不是洁癖 —— 内存版在多 worker / 重启后失效**且不报错**，
    一旦有人为了「省一次查询」把它加回来，出问题时几乎不可能被想到。
    """

    @pytest.mark.parametrize(
        "name",
        ["revoke_token", "is_token_revoked", "revoked_count", "clear_revoked"],
    )
    def test_security_has_no_memory_blacklist_api(self, name: str) -> None:
        assert not hasattr(security, name), (
            f"security.{name} 是已废弃的进程内存黑名单实现（R-03 已迁库），"
            "请改用 app.dao.token_dao"
        )
