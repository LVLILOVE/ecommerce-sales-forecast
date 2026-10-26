"""注册限流（`R-07`）的不变量测试。

为什么单测能覆盖到这里：`_assert_register_allowed()` 的两个数据源都被
刻意收敛成了「一次查询 + 一个字典」，所以在不建库、不起服务的前提下，
用 `monkeypatch` 替换 `oplog_dao.count_recent` 就能完整验证两层逻辑。

⚠️ 这套测试守的是**参数语义**，不是「有没有调用限流函数」：
   - 库层额度（`register_max_success`）用完 → 40104，且 `retry_after_seconds`
     必须按「窗口内最早那条」推算，不能随便返回一个窗口长度；
   - 内存层额度（`register_max_attempts`）必须**含失败尝试**，
     因为 bcrypt 在入库前执行，只限成功数挡不住 CPU DoS。
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from app.core import config
from app.core.errors import BizError, ErrorCode
from app.services import auth_service


@pytest.fixture(autouse=True)
def _clean_attempts():
    """每个用例前清空内存计数，避免用例间互相污染。"""
    auth_service._REG_ATTEMPTS.clear()
    yield
    auth_service._REG_ATTEMPTS.clear()


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """用 `dataclasses.replace` 造一份临时配置。

    ⚠️ `Settings` 是 `frozen=True`，运行期**不能**改属性（改了直接
       `FrozenInstanceError`），只能整体替换 `config.settings` 这个模块级变量。
    """
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, **overrides)
    )


class TestAttemptLayer:
    """内存层：管「每一次尝试」，含注定失败的。"""

    def test_allows_up_to_limit_then_rejects(self, monkeypatch) -> None:
        _settings(monkeypatch, register_max_attempts=3, register_max_success=0)

        for _ in range(3):
            auth_service._assert_register_allowed(None, ip="1.2.3.4")

        with pytest.raises(BizError) as ei:
            auth_service._assert_register_allowed(None, ip="1.2.3.4")
        assert ei.value.code == ErrorCode.LOGIN_LOCKED

    def test_rejection_carries_retry_after(self, monkeypatch) -> None:
        _settings(monkeypatch, register_max_attempts=1, register_max_success=0)
        auth_service._assert_register_allowed(None, ip="1.2.3.4")
        with pytest.raises(BizError) as ei:
            auth_service._assert_register_allowed(None, ip="1.2.3.4")
        retry = ei.value.data.get("retry_after_seconds")
        assert isinstance(retry, int) and retry > 0, f"retry_after_seconds={retry!r}"

    def test_limits_are_per_ip(self, monkeypatch) -> None:
        """一个 IP 打满不能让别的 IP 跟着被拒 —— 否则等于给自己做了个 DoS。"""
        _settings(monkeypatch, register_max_attempts=1, register_max_success=0)
        auth_service._assert_register_allowed(None, ip="1.2.3.4")
        auth_service._assert_register_allowed(None, ip="5.6.7.8")  # 不应抛

    def test_window_expiry_resets_counter(self, monkeypatch) -> None:
        base = datetime(2026, 10, 26, 12, 0, 0)
        monkeypatch.setattr(auth_service, "_now", lambda: base)
        _settings(
            monkeypatch,
            register_max_attempts=1,
            register_max_success=0,
            register_window_seconds=60,
        )
        auth_service._assert_register_allowed(None, ip="1.2.3.4")
        with pytest.raises(BizError):
            auth_service._assert_register_allowed(None, ip="1.2.3.4")

        # 窗口滑过去之后必须重新放行，否则等于永久封禁
        monkeypatch.setattr(auth_service, "_now", lambda: base + timedelta(seconds=61))
        auth_service._assert_register_allowed(None, ip="1.2.3.4")

    def test_disabled_when_zero(self, monkeypatch) -> None:
        """`0` = 关闭该层（运维可临时放开），不能变成「一次都不许注册」。"""
        _settings(monkeypatch, register_max_attempts=0, register_max_success=0)
        for _ in range(50):
            auth_service._assert_register_allowed(None, ip="1.2.3.4")


class TestSuccessLayer:
    """库层：管「真实建出来的账号数」，抗重启。"""

    def test_rejects_when_quota_exhausted(self, monkeypatch) -> None:
        _settings(monkeypatch, register_max_success=2, register_max_attempts=0)
        now = datetime(2026, 10, 26, 12, 0, 0)
        monkeypatch.setattr(auth_service, "_now", lambda: now)
        # 窗口内已有 2 条成功注册，最早一条发生在 600 秒前
        oldest = (now - timedelta(seconds=600)).strftime(auth_service.DATETIME_FMT)
        monkeypatch.setattr(
            auth_service.oplog_dao,
            "count_recent",
            lambda *a, **kw: (2, oldest),
        )

        with pytest.raises(BizError) as ei:
            auth_service._assert_register_allowed(None, ip="1.2.3.4")
        assert ei.value.code == ErrorCode.LOGIN_LOCKED
        # 3600 秒窗口 - 已过 600 秒 = 3000 秒后解锁
        assert ei.value.data["retry_after_seconds"] == pytest.approx(3000, abs=2)

    def test_passes_when_below_quota(self, monkeypatch) -> None:
        _settings(monkeypatch, register_max_success=2, register_max_attempts=0)
        monkeypatch.setattr(
            auth_service.oplog_dao, "count_recent", lambda *a, **kw: (1, None)
        )
        auth_service._assert_register_allowed(None, ip="1.2.3.4")  # 不应抛


class TestLayering:
    def test_success_layer_runs_before_attempt_layer(self, monkeypatch) -> None:
        """库层先判：额度已满时**不得**消耗内存层的尝试计数。

        理由：库层是硬额度，耗掉了也放不进来；而尝试计数是有限的
        （30 次/小时），被硬额度白白吃掉会让正常用户更快被锁。
        """
        _settings(monkeypatch, register_max_success=0, register_max_attempts=5)
        monkeypatch.setattr(
            auth_service.oplog_dao, "count_recent", lambda *a, **kw: (0, None)
        )
        auth_service._assert_register_allowed(None, ip="9.9.9.9")
        assert auth_service._REG_ATTEMPTS["9.9.9.9"][0] == 1

        # 换成「额度已满」，确认一次都没再计入
        _settings(monkeypatch, register_max_success=1, register_max_attempts=5)
        monkeypatch.setattr(
            auth_service.oplog_dao, "count_recent", lambda *a, **kw: (1, None)
        )
        for _ in range(3):
            with pytest.raises(BizError):
                auth_service._assert_register_allowed(None, ip="9.9.9.9")
        assert auth_service._REG_ATTEMPTS["9.9.9.9"][0] == 1, "库层拒绝不应消耗尝试计数"
