"""错误码体系的不变量测试。

这些断言守护的是「前端按 `body.code` 分支」这一整套设计的前提：
一旦出现**重复错误码**或**区间错位**，前端会静默走错分支——而这类 bug
在端到端断言里极难发现（接口都返回 200，body 也"看着正常"）。

另外钉住 `_HTTP_401_CODES` 这个**唯一例外**：只有 40101/40102 返回真实
HTTP 401，其余业务错误一律 HTTP 200 + `body.code`（见 `errors.py` 模块头注释）。
2026-10-26（`R-09`）例外扩成两类：`code >= 50000` 返真实 HTTP 500。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import (
    _HTTP_401_CODES,
    _PASSWORD_FIELDS,
    BizError,
    ErrorCode,
    http_status_for,
    register_exception_handlers,
)


def _all_codes() -> dict[str, int]:
    return {
        name: value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, int)
    }


class TestErrorCodeTable:
    def test_codes_are_unique(self) -> None:
        codes = _all_codes()
        assert len(codes) >= 20, f"错误码数量异常：{len(codes)}"
        seen: dict[int, str] = {}
        dup: list[str] = []
        for name, value in codes.items():
            if value in seen:
                dup.append(f"{seen[value]} 与 {name} 都是 {value}")
            seen[value] = name
        assert not dup, "错误码重复（前端分支会串）：" + "; ".join(dup)

    def test_codes_use_five_digit_http_style(self) -> None:
        """约定：`<HTTP 语义三位><两位序号>`，如 40101 / 40012 / 50003。

        `OK = 0` 是唯一的成功码，不参与该约定（见下一条用例）。
        """
        bad = {
            name: value
            for name, value in _all_codes().items()
            if name != "OK" and not (10000 <= value <= 59999)
        }
        assert not bad, f"错误码不符合 5 位约定：{bad}"

    def test_ok_is_zero_and_unique(self) -> None:
        """前端全部按 `body.code === 0` 判成功，这个 0 不能挪。"""
        assert ErrorCode.OK == 0
        assert list(_all_codes().values()).count(0) == 1

    @pytest.mark.parametrize(
        ("prefix", "group"),
        [
            (400, "参数 / 上传"),
            (401, "认证"),
            (403, "权限"),
            (404, "不存在"),
            (500, "服务内部"),
        ],
    )
    def test_group_has_at_least_one_code(self, prefix: int, group: str) -> None:
        hits = [n for n, v in _all_codes().items() if v // 100 == prefix]
        assert hits, f"{group}（{prefix}xx）区间没有任何错误码"


class TestHttp401Exception:
    def test_only_two_codes_use_real_401(self) -> None:
        assert set(_HTTP_401_CODES) == {
            ErrorCode.UNAUTHORIZED,
            ErrorCode.TOKEN_EXPIRED,
        }, "BE-20 约定：只有「未登录 / Token 失效」返回真实 HTTP 401"

    def test_business_codes_do_not_use_401(self) -> None:
        """登录失败、限流等即使属 401xx 区间，也**不**返回真实 401（避免浏览器弹框）。"""
        for code in (ErrorCode.BAD_CREDENTIAL, ErrorCode.LOGIN_LOCKED,
                     ErrorCode.EMAIL_TAKEN):
            assert code not in _HTTP_401_CODES


class TestBizError:
    def test_message_is_preserved(self) -> None:
        err = BizError(ErrorCode.BAD_PARAM, "参数不合法")
        assert err.code == ErrorCode.BAD_PARAM
        assert err.message == "参数不合法"
        assert str(err) == "参数不合法"

    def test_data_defaults_to_dict_not_none(self) -> None:
        """响应体里 `data` 必须是对象，不能是 null —— 否则前端解构会炸。"""
        assert BizError(ErrorCode.BAD_PARAM, "x").data == {}
        assert BizError(ErrorCode.BAD_PARAM, "x", {"f": 1}).data == {"f": 1}


class TestHttpStatusMapping:
    """`R-09`：`code >= 50000` → HTTP 500，其余业务码仍 200。

    这套映射是「前端一套 `body.code` 分支」与「运维按 5xx 告警」之间的
    唯一折中点。它一旦被改成「全部真实状态码」，前端 8 处分支和 38 套
    `verify_*` 会同时失效 —— 用测试钉住，避免以后被人"顺手优化"掉。
    """

    @pytest.mark.parametrize(
        "code", [ErrorCode.UNAUTHORIZED, ErrorCode.TOKEN_EXPIRED]
    )
    def test_auth_codes_use_401(self, code: int) -> None:
        assert http_status_for(code) == 401

    @pytest.mark.parametrize(
        "code", [ErrorCode.INFER_FAIL, ErrorCode.IO_ERROR, ErrorCode.DB_ERROR]
    )
    def test_server_fault_codes_use_500(self, code: int) -> None:
        assert http_status_for(code) == 500

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.MISSING_COLUMN,
            ErrorCode.WEAK_PASSWORD,
            ErrorCode.BAD_CREDENTIAL,
            ErrorCode.FORBIDDEN,
            ErrorCode.TASK_NOT_FOUND,
        ],
    )
    def test_client_side_codes_stay_200(self, code: int) -> None:
        """⚠️ 40301/40401 也必须是 200 —— 它们**不是** 5xx，无需告警。"""
        assert http_status_for(code) == 200

    def test_threshold_boundary(self) -> None:
        assert http_status_for(49999) == 200
        assert http_status_for(50000) == 500

    def test_error_code_table_has_no_code_above_59999(self) -> None:
        """`code >= 60000` 会落进「既非 4xx 也非 5xx」的无人区，映射会静默变 500。"""
        bad = {n: v for n, v in _all_codes().items() if v > 59999}
        assert not bad, f"错误码越过 5xxxx 区间：{bad}"


class TestHandlerEndToEnd:
    """走真实处理器链路：`BizError` → `JSONResponse(status=…)`，而不是只测纯函数。"""

    @staticmethod
    def _client() -> TestClient:
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/boom")
        async def boom():  # noqa: ANN202 - 测试夹具
            raise BizError(ErrorCode.IO_ERROR, "原始数据文件已丢失，请重新上传")

        @app.get("/bad")
        async def bad():  # noqa: ANN202
            raise BizError(ErrorCode.BAD_PARAM, "参数不合法")

        return TestClient(app)

    def test_server_fault_returns_500_with_body_code(self) -> None:
        with self._client() as c:
            r = c.get("/boom")
        assert r.status_code == 500
        # 🔒 body 仍是标准信封：状态码变了，前端解析路径不能跟着变
        assert r.json()["code"] == ErrorCode.IO_ERROR
        assert r.json()["message"]

    def test_business_fault_still_returns_200(self) -> None:
        with self._client() as c:
            r = c.get("/bad")
        assert r.status_code == 200
        assert r.json()["code"] == ErrorCode.BAD_PARAM


class TestPasswordFieldMapping:
    def test_both_password_fields_are_covered(self) -> None:
        """改密接口用 `new_password`，注册用 `password` —— 漏一个就报错码错位。"""
        assert set(_PASSWORD_FIELDS) == {"password", "new_password"}
