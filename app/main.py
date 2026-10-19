"""
=============================================================================
FastAPI 应用入口
-----------------------------------------------------------------------------
版本     : V1.0（EP-0，EP-5 增补模型预加载 / 卸载）
依据文档 : 《开发技术文档 V1.0》§4.1、§4.1.1（启动自检清单）、§4.7.3（模型单例）
-----------------------------------------------------------------------------
承担四件事：注册中间件、挂载路由、启动自检、管理模型单例。

进度：
  - 中间件链 + 路由 + 日志          → EP-0
  - init_db() + 启动自检            → EP-1 / EP-2
  - load_active_model() / clear()   → EP-5
=============================================================================
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.v1.router import api_router
from app.core import config
from app.core.db import init_db
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import AccessLogMiddleware, RequestIDMiddleware
from app.core.model_holder import holder, load_active_model

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------- 启动 ----------
    configure_logging()

    # 🔒 BE-05：密钥缺失或过短必须**启动失败**，而不是带着空密钥跑起来。
    #    若容错放行，服务能启动、能登录，但任何人拿公开的默认密钥即可伪造
    #    Token —— 这类「看起来正常」的配置错误最难在测试期发现。
    #    放在最前面：不做任何 I/O 就把配置问题暴露出来，排查成本最低。
    config.settings.ensure_secret_configured()

    # 建库建表（幂等）+ PRAGMA（WAL / 外键 / busy_timeout）
    # 直接执行 code/db/schema.sql，不用 Base.metadata.create_all()
    # —— 后者建不出视图、触发器与游客哨兵行（见 core/db.py 的设计说明）。
    init_db(run_seed=True)

    # 预加载启用模型（`BE-15` / §4.1.1 启动自检清单）。
    # 🔒 失败即启动失败，不做降级重试：
    #    ① `BE-06` 配置漂移（`.env` 的 ACTIVE_MODEL 与 model_registry.is_active=1
    #       的版本号不一致）必须在这里暴露 —— 否则会形成「服务正常、接口 200，
    #       但每次预测都用错版本」的静默故障，只有对比结果才会发现；
    #    ② `BE-15` 首次加载要读权重并建计算图（数秒），若放到首个请求里，
    #       这笔开销会被计进该请求的 `duration_ms`，直接污染性能指标。
    #    ⚠️ 顺序放在 init_db() **之后**：模型注册信息来自数据库，库没建好时
    #       校验会误报「model_registry 中没有启用记录」。
    load_active_model()
    logger.info(
        "模型已预加载 version=%s device=%s params=%s",
        holder.version,
        holder.device,
        holder.handle.param_count if holder.handle else 0,
    )

    logger.info(
        "应用启动完成 env=%s db=%s", config.settings.app_env, config.settings.db_path
    )
    yield
    # ---------- 关闭 ----------
    # 释放模型引用。显式清空的意义在于「让持有状态与进程生命周期一一对应」：
    # 测试用例会在同一进程内反复进出 lifespan（TestClient 上下文），
    # 若不清空，上一个用例加载的模型会残留在单例里，
    # 使「未加载模型时应返回 40404」这类断言依赖执行顺序而随机通过。
    holder.clear()


# ---------------------------------------------------------------------------
# Swagger UI / ReDoc 资源离线化（`DEP-4` /《开发技术文档》§10.4）
# ---------------------------------------------------------------------------
# 🔒 为什么必须自带资源：FastAPI 的 `docs_url="/docs"` 默认从
#    `cdn.jsdelivr.net` 加载 Swagger UI 的 JS 与 CSS。国内受限网络下 CDN 被重置，
#    表现为 —— HTML 能拿到（200），但 `.opblock` 一个都渲染不出来（白屏）。
#    这类故障在能联网的开发机上**完全看不出来**，只有断网/被墙环境才暴露，
#    因此不能拿「本地能打开」当验收依据。
#
# 做法：随仓库自带 `swagger-ui-bundle.js` / `swagger-ui.css` / favicon 与
#      `redoc.standalone.js`（版本见下方 SWAGGER_VERSION / REDOC_VERSION），
#      用 get_swagger_ui_html / get_redoc_html 显式指向本地路径。
#
# 版本：swagger-ui-dist 5.33.1 / redoc 2.1.5（2026-10 取自 npm 官方发布）
# 许可：MIT（swagger-ui-bundle.js.LICENSE.txt 随资源一并归档）
SWAGGER_VERSION = "5.33.1"
REDOC_VERSION = "2.1.5"

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_SWAGGER_DIR = _STATIC_DIR / "vendor" / "swagger-ui"
_REDOC_DIR = _STATIC_DIR / "vendor" / "redoc"

# 目录被删就自动回退 FastAPI 默认行为（走 CDN），而不是启动即崩 —— 便于裁剪部署。
_SWAGGER_LOCAL = (_SWAGGER_DIR / "swagger-ui-bundle.js").is_file() and (
    _SWAGGER_DIR / "swagger-ui.css"
).is_file()
_REDOC_LOCAL = (_REDOC_DIR / "redoc.standalone.js").is_file()

app = FastAPI(
    title="电商销量预测系统 API",
    version=config.APP_VERSION,
    lifespan=lifespan,
    # FR-SYS-02 的接口文档交付物。本地资源齐备时改为自定义路由（见下），
    # 故显式关掉 FastAPI 内置的 CDN 版本，避免出现「两个 /docs」。
    docs_url=None if _SWAGGER_LOCAL else "/docs",
    redoc_url=None if _REDOC_LOCAL else "/redoc",
)

if _SWAGGER_LOCAL:
    app.mount(
        "/static/vendor/swagger-ui",
        StaticFiles(directory=str(_SWAGGER_DIR)),
        name="swagger-ui",
    )

    @app.get("/docs", include_in_schema=False)
    async def api_docs():  # pragma: no cover - 渲染结果由浏览器走查脚本断言
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=app.title + " · 接口文档",
            swagger_js_url="/static/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/vendor/swagger-ui/favicon-32x32.png",
        )

if _REDOC_LOCAL:
    app.mount(
        "/static/vendor/redoc",
        StaticFiles(directory=str(_REDOC_DIR)),
        name="redoc",
    )

    @app.get("/redoc", include_in_schema=False)
    async def api_redoc():  # pragma: no cover - 渲染结果由浏览器走查脚本断言
        return get_redoc_html(
            openapi_url=app.openapi_url,
            title=app.title + " · 接口文档（ReDoc）",
            redoc_js_url="/static/vendor/redoc/redoc.standalone.js",
            with_google_fonts=False,  # 🔒 关掉 ReDoc 默认的 Google Fonts 外链
            # 🔒 FastAPI 的 `get_redoc_html` 默认 favicon 指向
            #    `https://fastapi.tiangolo.com/img/favicon.png` —— 这是一个
            #    **藏在默认参数里的外链**，只关 `with_google_fonts` 并不能除干净。
            #    `verify_docs_browser.mjs` 的 ②-3 断言正是为此而设（EP-10 实测命中）。
            # ⚠️ 参数名是 `redoc_favicon_url`，不是 `favicon_url`
            #    （Swagger 侧才叫 `swagger_favicon_url`；写错会 TypeError → 500）。
            redoc_favicon_url="/static/vendor/swagger-ui/favicon-32x32.png",
        )

# 中间件链（执行顺序自上而下：CORS → RequestID → AccessLog）
# 注意：Starlette 的 add_middleware 是「后添加的在外层」，故此处按相反顺序注册，
# 使 RequestIDMiddleware 包裹 AccessLogMiddleware —— 否则访问日志记录时 request_id
# 已被 reset，日志中的 [request_id] 恒为 "-"（EP-0 实测踩坑）。
app.add_middleware(AccessLogMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(config.settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

# 路由挂载：health 挂根路径；业务路由统一在 /api/v1 下
app.include_router(health_router)
app.include_router(api_router, prefix="/api/v1")


# =============================================================================
# 单端口托管前端（`SERVE_FRONTEND=1` 时启用）
# -----------------------------------------------------------------------------
# 用途：把「前端静态产物 + 后端 API」放到**同一个端口**上，适用于没有
#   Nginx 的场景（如平台托管、快速发布）。前端与 API 同源 ⇒ 不需要代理，
#   也不需要为部署再配一次 CORS。
#
# ⚠️ 默认 **关闭**：既有的《部署运维手册》形态是 Nginx 托管静态 + 反向代理
#    `/api`，此时后端应当只监听 API。默认关闭也能保证**现有 verify_* 校验
#    脚本的行为完全不变**（它们跑在 APP_ENV=development 下）。
#
# ⚠️ 必须挂在**所有路由注册之后**：Starlette 按注册顺序匹配，这条 catch-all
#    放在最后才不会抢走 `/api/v1/*`、`/health`、`/docs`。
# =============================================================================
_FRONTEND_DIR = Path(config.settings.frontend_dir)
_FRONTEND_INDEX = _FRONTEND_DIR / "index.html"
_FRONTEND_ASSETS = _FRONTEND_DIR / "assets"
_FRONTEND_READY = (
    config.settings.serve_frontend
    and _FRONTEND_INDEX.is_file()
    and _FRONTEND_ASSETS.is_dir()
)

if config.settings.serve_frontend and not _FRONTEND_READY:
    # 显式告警而不是静默跳过：配了 `SERVE_FRONTEND=1` 却看不到页面时，
    # 十有八九是产物没构建或 `FRONTEND_DIR` 指错 —— 日志里要能直接看出来。
    logger.warning(
        "SERVE_FRONTEND 已开启但前端产物不完整，已跳过静态托管。"
        "期望 index.html 与 assets/ 位于 %s",
        _FRONTEND_DIR,
    )

if _FRONTEND_READY:
    app.mount(
        "/assets",
        StaticFiles(directory=str(_FRONTEND_ASSETS)),
        name="frontend-assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:  # pragma: no cover
        """SPA 兜底：未命中任何已注册路由的 GET 一律回 `index.html`。

        前端用的是 `BrowserRouter`（history 路由），刷新 `/result/12` 这类
        子路由时浏览器会直接向服务端请求该路径 —— 没有这条兜底就是 404。

        🔒 **`Cache-Control: no-store` 不可省**（2026-10-19 线上故障）：
        `index.html` 引用的是**带 hash 的构建产物**（`assets/index-<hash>.js`）。
        每次重新发布会换掉 hash，旧的 JS 文件随即消失。若浏览器缓了旧
        `index.html`，它仍会去请求**已不存在的旧 hash** → 404 → 页面白屏，
        用户看到的就是「打不开」。缓存 HTML 没有任何收益（它只有 437 B），
        所以直接禁用缓存；带 hash 的 `/assets/*` 仍可长缓存。
        """
        return FileResponse(
            _FRONTEND_INDEX,
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    logger.info("已启用单端口托管前端：%s", _FRONTEND_DIR)
