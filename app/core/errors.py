"""
=============================================================================
业务异常类 + 统一异常处理器
-----------------------------------------------------------------------------
版本     : V1.0（EP-0）
依据文档 : 《开发技术文档 V1.0》§4.5（统一异常处理）、§5.1.1（统一响应包）、§5.2（错误码全表）
-----------------------------------------------------------------------------
关键约定：
  - BE-20 🔒：业务错误一律 HTTP 200 + body.code 区分（FR-SYS-01 的字面要求）。
    例外只有两类：
      ① HTTP 401 —— 需真实状态码以触发前端重新认证；
      ② 服务端故障码（code >= 50000）—— 见下方「R-09」说明。
  - API-00-9：401xx 中仅 40101（未登录）/ 40102（Token 失效）返回真实 401，
    二者由 get_current_user 抛出；登录失败（40103/40104）发生在免鉴权的登录
    接口上，仍按 HTTP 200 + body.code 返回。
  - R-09 🔒（2026-10-26）：**`code >= 50000` 一律返回真实 HTTP 500**。
    动机是「可观测性」而非「更 REST」—— 统一 200 时 Nginx/ALB/APM 的 5xx
    告警规则对本站**完全失效**，`curl -f` 也退化成必须人工解析 body。
    折中方式是**只加 5xx 映射**：4xxxx（含 403/404 语义码）继续保持 200，
    这样前端「一套 body.code 分支」的收益完全保留，也让本仓 38 套
    `verify_*` 脚本的既有断言不受影响。
    ⚠️ 客户端要注意：`code >= 50000` 的响应**不再进 axios 的 success 分支**，
    `client.ts` 的响应拦截器必须从 `error.response.data` 里把 code/message
    取回来（blob 下载路径还要先解 JSON）。
  - BE-21 ⚠️：兜底处理器不向用户泄露堆栈/原始异常文本，只返回固定文案。
  - EP-2 修复 🔒：校验错误载荷必须先经 `_safe_errors()` 消毒再序列化。
    pydantic v2 在字段级校验器抛 `ValueError` 时会把**异常对象**放进
    `error['ctx']`，直接 `json.dumps` 会 `TypeError` —— 于是「密码强度不足」
    这个最需要友好提示的场景反而得到 500（与 `RG-06` 同型的失败）。
=============================================================================
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class BizError(Exception):
    """业务异常。code 为 PRD 定义的业务错误码（400xx~500xx）。"""

    def __init__(self, code: int, message: str, data: dict | None = None):
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(message)


class ErrorCode:
    """业务错误码常量（与 code/frontend/types.ts 的 ERR 同源，改动须同步）。"""

    OK = 0

    # 400xx 请求 / 数据校验
    MISSING_COLUMN = 40001
    DATE_UNPARSABLE = 40002
    NEGATIVE_VALUE = 40003
    SERIES_OVER_LIMIT = 40004
    DATE_GAP = 40005
    INSUFFICIENT_DATA = 40006
    FILE_TOO_LARGE = 40007
    ROW_OVER_LIMIT = 40008
    BAD_EXTENSION = 40009
    EMPTY_FILE = 40010
    WEAK_PASSWORD = 40011
    BAD_PARAM = 40012

    # 401xx 认证
    UNAUTHORIZED = 40101
    TOKEN_EXPIRED = 40102
    BAD_CREDENTIAL = 40103
    LOGIN_LOCKED = 40104
    EMAIL_TAKEN = 40105

    # 403xx 权限
    FORBIDDEN = 40301

    # 404xx 不存在
    TASK_NOT_FOUND = 40401
    DATASET_NOT_FOUND = 40402
    USER_NOT_FOUND = 40403
    MODEL_NOT_READY = 40404

    # 500xx 服务内部
    INFER_FAIL = 50001
    IO_ERROR = 50002
    DB_ERROR = 50003


# 需要返回真实 HTTP 401 的错误码（BE-20 例外 ①）
_HTTP_401_CODES = (ErrorCode.UNAUTHORIZED, ErrorCode.TOKEN_EXPIRED)

# 需要返回真实 HTTP 500 的错误码下界（BE-20 例外 ②，见模块头 R-09）
_HTTP_5XX_FLOOR = 50000


def http_status_for(code: int) -> int:
    """把业务错误码映射成 HTTP 状态码（**唯一映射入口**，改动只改这里）。

    | 输入 | 输出 | 理由 |
    |---|---|---|
    | 40101 / 40102 | 401 | 触发前端重新认证 |
    | >= 50000 | 500 | 5xx 告警 / `curl -f` 可用 |
    | 其余（含 40301/40401） | 200 | 保住「一套 `body.code` 分支」 |

    刻意**不**把 40301 映射成 403、40401 映射成 404：那需要同步改前端
    的 8 处分支与 38 套校验脚本，收益（更"像" REST）远小于代价。
    """
    if code in _HTTP_401_CODES:
        return 401
    if code >= _HTTP_5XX_FLOOR:
        return 500
    return 200

# 密码类字段：其校验失败应映射为 40011（密码强度不足），而不是通用的 40012。
# 依据技术文档 §5.4 的 API-01 错误码表：40011 = 密码强度不足。
# 这三类 type 分别对应「字段级校验器抛 ValueError」「长度不足」「长度超限」，
# 都属强度问题，用户看到的修正建议相同，不必拆成三个码。
_PASSWORD_FIELDS = ("password", "new_password")
_PASSWORD_ERROR_TYPES = ("value_error", "string_too_short", "string_too_long")


def _safe_errors(exc: RequestValidationError) -> list[dict]:
    """把 pydantic 的 errors() 转成**保证可 JSON 序列化**的结构。

    🔒 这不是防御性编程洁癖，而是一个已复现的缺陷修复（EP-2）：
       pydantic v2 的 `errors()` 在字段级校验器抛 `ValueError` 时，会在
       `ctx` 里带上**异常对象本身**（`{"error": ValueError(...)}`）。
       `JSONResponse` 内部走 `json.dumps`，遇到非基本类型直接
       `TypeError: Object of type ValueError is not JSON serializable`
       —— 异常处理器自己抛异常，请求以原始 500 结束。

       后果特别严重的一点是：`schemas.py` 里**恰恰**有若干个抛
       `ValueError` 的校验器（密码强度、72 字节上限、horizon 取值…），
       也就是说「密码不合法」这个最常见、最需要友好提示的场景，
       会从预期的 `40011` 变成 500 —— 与本项目 `RG-06`
       「bcrypt 72 字节导致 500」是同一种失败形态，只是入口换到了错误处理器。

    处理方式：剥掉 `url`（对用户无意义），并把 `ctx` 中的异常对象降级为字符串。
    """
    out: list[dict] = []
    for err in exc.errors():
        item = {k: v for k, v in err.items() if k not in ("ctx", "url")}
        ctx = err.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {
                k: (str(v) if isinstance(v, BaseException) else v) for k, v in ctx.items()
            }
        out.append(item)
    return out


def register_exception_handlers(app: FastAPI) -> None:
    """注册三类异常处理器：业务异常 / 参数校验失败 / 未捕获异常。"""

    @app.exception_handler(BizError)
    async def _biz(_, exc: BizError):
        status = http_status_for(exc.code)
        # 5xx 说明是「我们的错」而不是「用户的错」：必须留一条 error 级日志，
        # 否则接入 APM 后只能看到状态码、定位不到是哪条业务分支。
        if status >= 500:
            logger.error(
                "服务端业务异常 code=%s message=%s data_keys=%s",
                exc.code,
                exc.message,
                sorted((exc.data or {}).keys()),
            )
        return JSONResponse(
            status_code=status,
            content={"code": exc.code, "message": exc.message, "data": exc.data},
        )

    @app.exception_handler(RequestValidationError)
    async def _pydantic(_, exc: RequestValidationError):
        try:
            raw = exc.errors()
            first = raw[0]
            field = ".".join(str(x) for x in first["loc"][1:]) or "body"
            detail = _safe_errors(exc)
            message = f"参数 {field} 不合法：{first['msg']}"
            # 密码类字段的校验失败归到 40011（契约要求），其余为 40012。
            code = (
                ErrorCode.WEAK_PASSWORD
                if field in _PASSWORD_FIELDS and first.get("type") in _PASSWORD_ERROR_TYPES
                else ErrorCode.BAD_PARAM
            )
        except Exception:  # noqa: BLE001 - 兜底：处理器自身绝不能再抛
            logger.exception("校验错误处理器构造响应失败")
            field, detail, message, code = (
                "body",
                [],
                "请求参数不合法，请检查后重试",
                ErrorCode.BAD_PARAM,
            )

        return JSONResponse(
            status_code=200,
            content={
                "code": code,
                "message": message,
                "data": {"field": field, "errors": detail},
            },
        )

    @app.exception_handler(Exception)
    async def _unknown(_, exc: Exception):
        logger.exception("未捕获异常", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INFER_FAIL,
                "message": "服务内部错误，请稍后重试或联系管理员",
                "data": {},
            },
        )
