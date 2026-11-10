"""SSE 实时事件流。

为什么是 SSE 而不是 WebSocket：C 只需要单向接收，SSE 是浏览器原生支持的
HTTP 流，不需要额外的连接管理、心跳和重连逻辑（`EventSource` 自带重连）。
代价是不能双向，但 C→A 的操作都是普通 HTTP 请求，本来也不需要双向。

两个部署上的坑：

- **Nginx 必须关掉对这条路径的 buffering**，否则事件被攒在代理层，前端看到
  的进度是一段一段跳的，最后一帧可能永远不出现。
- **事件不重放。** 断线期间的 `task_done` 会丢，所以前端重连后必须拉一次
  `GET /tasks` 对账。这是协议层面的约定，不是可选的优化。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..deps import Context, SessionToken

router = APIRouter(tags=["事件"])

KEEPALIVE_SECONDS = 15


@router.get("/api/events")
async def event_stream(request: Request, ctx: Context, token: SessionToken) -> StreamingResponse:
    queue = ctx.bus.subscribe()

    async def generator() -> AsyncIterator[str]:
        try:
            # 告诉浏览器断线后 3s 再连
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    # 注释行是合法的 SSE 心跳，不会触发 onmessage
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            ctx.bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Nginx 见到这个头会关掉自己的缓冲
            "X-Accel-Buffering": "no",
        },
    )
