"""
=============================================================================
中间件链
-----------------------------------------------------------------------------
版本     : V1.0（EP-0）
依据文档 : 《开发技术文档 V1.0》§4.2 中间件链
-----------------------------------------------------------------------------
执行顺序（自上而下）：
  1. CORSMiddleware     跨域（在 main.py 注册）
  2. RequestIDMiddleware  为每个请求生成 request_id，注入日志上下文
  3. AccessLogMiddleware  记录方法 / 路径 / 耗时 / 状态码（不记录请求体，防泄露）

🔴 **没有第 4 层「TimeoutGuardMiddleware」**（2026-10-26 复评更正）：
   本文件的旧 docstring 与开发技术文档 §4.2 的中间件表都列了它，
   但**代码里从来没有这个类**。而紧邻着 `BE-16` 的正文写的才是正确方案：
   「**超时不能靠中间件强杀**……正确做法是**在编排层做软失败**：
   `predict_service` 内部维护 `deadline`，在每个阶段结束时检查」。
   —— 这也符合 FastAPI 的现实：端点是**同步阻塞**跑的，ASGI 中间件
   拿到控制权时请求已经结束，它**没有能力**中断一个正在跑的同步任务；
   强杀只会留下 `status=running` 的僵尸任务。
   现在的实现：`predict_service._check_deadline()` 在两遍流水线之间、
   CPU 回退重跑前、构建预测行后各查一次，超预算 → `E_TIMEOUT` 软失败。
=============================================================================
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求生成短 request_id，贯穿该请求的全部日志。"""

    async def dispatch(self, request: Request, call_next):
        token = request_id_var.set(uuid.uuid4().hex[:12])
        try:
            return await call_next(request)
        finally:
            request_id_var.reset(token)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """访问日志：方法、路径、状态码、耗时。不记录请求体（NFR-SEC-09）。

    🔴 **结构化 + 带 `task_id` / `user_id`**（2026-10-26 复评修复）：
    旧实现是纯文本模板 `"%s %s -> %s %.1fms"`，只有 method/path/status/elapsed
    四个字段 —— 排查「某个 task 为什么慢」时要跨两个日志源做字符串匹配，
    接 ELK/Loki 后也无法按 `task_id` 聚合。

    现在改为**单行 JSON**（日志行里 `request_id` 之后的部分就是 JSON），
    并从 `request.state` 取**可选**的 `task_id` / `user_id`
    （由鉴权后的路由层设置，中间件自己不解析 Token —— 那既是性能开销，
    也会把「鉴权失败」和「未鉴权」混为一谈）。
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        payload = {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        # 🔴 可选字段：只有路由层显式设置过才有值，缺省就不出现
        task_id = getattr(request.state, "task_id", None)
        if task_id is not None:
            payload["task_id"] = task_id
        user_id = getattr(request.state, "user_id", None)
        if user_id is not None:
            payload["user_id"] = user_id
        logger.info("%s", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return response
