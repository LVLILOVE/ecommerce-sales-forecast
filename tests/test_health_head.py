"""`HEAD` 请求必须与 `GET` 同样可用。

为什么单独一个文件（2026-10-26 线上实测）
-----------------------------------------------------------------------------
托管平台/桌面面板的可用性预检、链接预览和部分爬虫**先发 `HEAD`**。
FastAPI 的 `@router.get(...)` / `@app.get(...)` **不会**顺带注册 `HEAD`
（Starlette 的 `Route` 会自动加，`APIRoute` 不会），于是 `HEAD /` 与
`HEAD /health` 都返回 **405 Method Not Allowed**。

上层把 405 当成「服务不可用」，直接渲染自己的 404 页 —— 用户看到的是
**「打不开，Not Found」**，而我们这边 `curl`（GET）永远 200，日志里也
只有正常的 GET 记录。这类「对用户是 404、对开发者是 200」的分歧极难自证。

所以：**凡是浏览器/平台会直接访问的路径，都要能应答 HEAD**。
本文件守两条最关键的（`/` 与 `/health`），并顺手守住「别把 HEAD 加进 OpenAPI」
（`verify_ep9_contract.py` 核对 60 个操作，多一个 HEAD 操作会被判契约漂移）。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health import router as health_router


def _client() -> TestClient:
    """只装 health 路由：不触发 lifespan（不建库、不加载模型），毫秒级。"""
    app = FastAPI()
    app.include_router(health_router)
    return TestClient(app)


class TestHealthHead:
    def test_get_ok(self) -> None:
        with _client() as c:
            r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["code"] == 0

    def test_head_ok(self) -> None:
        with _client() as c:
            r = c.head("/health")
        assert r.status_code == 200, "HEAD 必须与 GET 同码（平台预检会先发 HEAD）"
        # HEAD 的语义：头齐全、体为空
        assert r.content == b""
        assert r.headers.get("content-length") not in (None, "0")

    def test_head_not_in_openapi(self) -> None:
        """`include_in_schema=False`：契约里仍然只有 GET。"""
        with _client() as c:
            schema = c.get("/openapi.json").json()
        methods = set(schema["paths"]["/health"].keys())
        assert methods == {"get"}, f"/health 的 OpenAPI 方法集被改了：{methods}"
