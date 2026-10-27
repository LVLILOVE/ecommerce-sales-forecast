"""导出响应的**唯一**构造点（`API-16` 结果导出 / `API-19a` 历史导出 共用）。

为什么单独抽出来
-----------------------------------------------------------------------------
`FR-HIS-03` 要求「历史记录导出，列定义同 `FR-VIS-05`」——
两条路由的导出**内容与文件名规则完全一致**，只差鉴权后的归属校验
（`prepare_export` 内部已经做了 `_owned_task`）。

如果两处各写一遍响应头，迟早会分叉，而这里有两个**不能写错**的细节：

1. 🔒 **文件名双写**（`API-16-3`）：`filename*=UTF-8''<percent-encoded>`
   是 RFC 5987 标准写法，中文名在各浏览器下都不会乱码；同时保留 ASCII 兜底
   `filename="..."`，供不认识 `filename*` 的老客户端退回 `task_42_export.csv`，
   而不是拿到乱码名或直接 404。
2. 🔒 **不设 `Content-Length`**：内容是 `StreamingResponse` 产出的生成器，
   长度在生成完毕前未知，由 chunked 传输。硬塞一个长度会让客户端提前截断。
3. 🔒 **`Cache-Control: no-store`**：导出含明细数据，任何中间层都不得缓存。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi.responses import StreamingResponse

from app.services import result_service


def export_response(spec: result_service.ExportSpec, task_id: int) -> StreamingResponse:
    """把 `ExportSpec` 包成一个可下载的流式响应。"""
    ascii_fallback = f"task_{task_id}_export.{spec.suffix}"
    quoted = quote(spec.name, safe="")
    return StreamingResponse(
        result_service.iter_export_chunks(spec),
        media_type=spec.mime,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"
            ),
            "Cache-Control": "no-store",
        },
    )


def client_ip(request) -> str | None:
    """客户端 IP（写审计日志用）。

    与 `api/v1/auth.py` / `datasets.py` / `predictions.py` 同一口径：
    **不读可伪造的 `X-Forwarded-For`**（网关会注入，但它本身也可被伪造，
    在不知道可信代理白名单的前提下信任它 = 允许绕过限流）。
    """
    return request.client.host if request.client else None
