"""第三轮新增功能的回归守卫（`FR-HIS-03` / `FR-ADM-01`）。

**H-1 历史记录导出（`FR-HIS-03`，P0）**
-----------------------------------------------------------------------------
PRD 要求「历史记录可导出（CSV / Excel），列定义同 `FR-VIS-05`」。
此前后端**没有这条路**（`history.py` 只有 list/detail/compare/delete），
前端历史页也**零导出入口** —— 也就是说结果页能导、回到历史就导不了，
而「回头导上次的结果」恰恰是历史功能的主要用途。

守卫方式：源码级 + 行为级。
- 源码级：路由已注册、复用同一个 `prepare_export`、前端有调用点；
- 行为级：`prepare_export` 对**不属于自己**的任务仍要 403（不能因为
  多开一条导出通道就把归属校验漏了）。

**H-2 埋点 16 事件（`FR-ADM-01`）**
-----------------------------------------------------------------------------
`event_dao.record_safe()` 早就存在、`events` 表也建了、管理端看板也接了
`count_by_name()` —— 但**全仓没有一处调用**，看板「埋点概览」永远为空。

守卫方式：
- 16 个常量必须**全部**定义且互不重复（少一个 = PRD 有一条没接）；
- 关键链路的源码里必须出现对应常量（防止接了一半）；
- 管理端计数必须**补齐全集**（缺的补 0），否则看板缺行会被误读成「没接」。
"""
from __future__ import annotations

import inspect

import pytest

from app.dao import event_dao
from app.models import event_names as en
from app.services import admin_service, auth_service, dataset_service
from app.services import history_service, predict_service, result_service


class TestHistoryExport:
    def test_route_registered(self) -> None:
        from app.api.v1 import history

        paths = {r.path for r in history.router.routes}
        assert "/history/{task_id}/export" in paths, "历史导出路由未注册"

    def test_reuses_same_exporter(self) -> None:
        """必须复用 `prepare_export` —— 否则两份列序必然漂移（PRD：列定义同 FR-VIS-05）。"""
        from app.api.v1 import history

        src = inspect.getsource(history.history_export)
        assert "result_service.prepare_export" in src, (
            "历史导出没有复用 prepare_export，列序可能与结果页不一致"
        )

    def test_requires_login(self) -> None:
        """导出是把数据带走，必须 `CurrentUser`（与历史详情同口径）。"""
        from app.api.v1 import history

        sig = inspect.signature(history.history_export)
        assert "user" in sig.parameters

    def test_ownership_still_enforced(self) -> None:
        """行为级：别人的任务不能被导出（新增通道不能绕过归属校验）。"""
        from unittest.mock import MagicMock

        from app.core.errors import BizError, ErrorCode

        db = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            def _boom(*_a, **_k):
                raise BizError(ErrorCode.FORBIDDEN, "无权访问")

            mp.setattr(result_service, "_owned_task", _boom)
            with pytest.raises(BizError) as exc:
                result_service.prepare_export(db, 1, user_id=999, fmt="csv")
            assert exc.value.code == ErrorCode.FORBIDDEN

    def test_frontend_calls_it(self) -> None:
        """契约守卫要求「后端每个路由都被前端消费」，这里也钉一道。"""
        from pathlib import Path

        src = Path("web/src/api/client.ts").read_text(encoding="utf-8")
        assert "/history/${taskId}/export" in src, "前端没有调用历史导出接口"


class TestEventInstrumentation:
    def test_sixteen_events_defined(self) -> None:
        assert len(en.ALL_EVENTS) == 16, f"PRD 定义了 16 个事件，实际 {len(en.ALL_EVENTS)}"
        assert len(set(en.ALL_EVENTS)) == 16, "事件名有重复"
        assert all(e.startswith("EV-") for e in en.ALL_EVENTS)
        assert set(en.EVENT_LABELS) == set(en.ALL_EVENTS), "EVENT_LABELS 与 ALL_EVENTS 不一致"

    @pytest.mark.parametrize(
        ("module", "attrs"),
        [
            (auth_service, ("EV_AUTH_01_REGISTER", "EV_AUTH_02_LOGIN", "EV_AUTH_03_LOGIN_FAIL")),
            (dataset_service, ("EV_DAT_01_UPLOAD", "EV_DAT_02_VALIDATE_FAIL", "EV_DAT_03_SAMPLE_LOAD")),
            (predict_service, ("EV_PRED_01_SUBMIT", "EV_PRED_02_COMPLETE",
                               "EV_PRED_03_FAIL", "EV_PRED_04_TIMEOUT",
                               "EV_PRED_05_DEVICE_FALLBACK")),
            (result_service, ("EV_VIS_01_RESULT_VIEW", "EV_VIS_02_RESULT_EXPORT",
                              "EV_VIS_03_COVERAGE_VIEW")),
            (history_service, ("EV_HIS_01_HISTORY_VIEW", "EV_HIS_02_HISTORY_COMPARE")),
        ],
    )
    def test_module_actually_records(self, module, attrs) -> None:
        """每个承载模块都必须**真的引用**自己的事件常量（不是只 import）。"""
        src = inspect.getsource(module)
        for attr in attrs:
            const = getattr(en, attr)
            assert f"event_dao.record_safe(" in src, f"{module.__name__} 没有调用 record_safe"
            assert const in src, f"{module.__name__} 没有记录 {attr}（{const}）"

    def test_log_appended_module_has_explicit_calls(self) -> None:
        """`EV-PRED-03/04` 写在 `_fail()` 单一落点里 —— 防止有人把埋点挪散。"""
        src = inspect.getsource(predict_service._fail)
        assert "EV_PRED_04_TIMEOUT" in src and "EV_PRED_03_FAIL" in src, (
            "_fail() 里没有按 error_code 分派失败/超时埋点 —— "
            "三处调用点会漏记"
        )

    def test_admin_counts_fill_zero(self) -> None:
        """管理端计数必须补齐 16 个 key（缺的补 0），否则看板缺行会被误读。"""
        src = inspect.getsource(admin_service.event_dao_counts)
        assert "ALL_EVENTS" in src, "埋点概览没有补齐事件全集"

    def test_admin_counts_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真的调一次：库里只有 1 个事件时，返回的字典仍要有 16 个 key。"""

        monkeypatch.setattr(
            event_dao, "count_by_name", lambda *_a, **_k: {"EV-AUTH-01": 3}
        )
        counts = admin_service.event_dao_counts(None, 7)  # db 不会被用到
        assert len(counts) == 16, f"应补齐 16 个 key，实际 {len(counts)}"
        assert counts["EV-AUTH-01"] == 3
        assert counts["EV-HIS-02"] == 0, "没有数据的事件必须补 0，而不是缺行"
