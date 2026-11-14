"""两条 WebSocket：观众看画面、cloud-agent 推画面。

为什么观众端也用 WebSocket 而不是 SSE/MJPEG：

- 视频是**双向失败**的：观众关页面要能立刻被发现，才能及时释放上游；
- 二进制帧走 WS 不需要 base64，带宽和延迟都更省；
- 浏览器端的解码器（WebCodecs）已经在机器人前端验证过，同一套帧格式可以复用。

鉴权分两套，刻意不混：

- 观众：浏览器 Cookie 会话，与现有页面同一份；
- 上游：agent token，只认 `Authorization` header（`deps.py` 明确不接受
  query token，避免 token 进访问日志）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..deps import bearer_from_header
from ..media import MediaError, MediaSession

logger = logging.getLogger("robot-monitor.live")

router = APIRouter()

_CLOSE_POLICY = 1008
_CLOSE_STOPPED = 1012
_CLOSE_UNAUTHORIZED = 4401


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@router.websocket("/ws/live/{agent_id}")
async def live_socket(websocket: WebSocket, agent_id: str) -> None:
    ctx = websocket.app.state.ctx
    # 不传编号 = 默认球形摄像头；云端不替硬件决定编号。
    camera_id = (websocket.query_params.get("cameraId") or "").strip()

    raw = websocket.cookies.get(ctx.settings.cookie_name)
    if await ctx.auth.verify_session(raw) is None:
        await websocket.close(code=_CLOSE_POLICY, reason="unauthorized")
        return

    await websocket.accept()
    await websocket.send_text(_json({"t": "session", "state": "connecting", "cameraId": camera_id}))

    try:
        session, viewer = await ctx.media.join(agent_id, camera_id, websocket)
    except MediaError as exc:
        await _send_error(websocket, exc.code, exc.message)
        await websocket.close(code=_CLOSE_STOPPED, reason=exc.message[:100])
        return
    except Exception as exc:  # noqa: BLE001 - 观众侧只该看到可读原因
        logger.exception("建立媒体会话失败")
        await _send_error(websocket, "internal", str(exc))
        await websocket.close(code=_CLOSE_STOPPED, reason="internal")
        return

    await ctx.auth.audit("web", "camera.view.start", target=agent_id, detail=f"{camera_id}:{session.sid}")

    sender = asyncio.create_task(_send_loop(websocket, viewer.queue), name="live-send")
    receiver = asyncio.create_task(_recv_loop(websocket), name="live-recv")
    try:
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        await ctx.media.leave(session, viewer)
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close()


@router.websocket("/ws/agent/media")
async def agent_media_socket(websocket: WebSocket) -> None:
    ctx = websocket.app.state.ctx
    row = await ctx.auth.authenticate_agent(
        bearer_from_header(websocket.headers.get("authorization"))
    )
    if row is None:
        await websocket.close(code=_CLOSE_UNAUTHORIZED, reason="invalid or revoked agent token")
        return

    agent_id = str(row["id"])
    # 不传编号 = 默认球形摄像头；云端不替硬件决定编号。
    camera_id = (websocket.query_params.get("cameraId") or "").strip()
    session_id = (websocket.query_params.get("sessionId") or "").strip()
    await websocket.accept()

    try:
        session = await ctx.media.attach_upstream(agent_id, camera_id, session_id, websocket)
    except MediaError as exc:
        await _send_error(websocket, exc.code, exc.message)
        await websocket.close(code=_CLOSE_STOPPED, reason=exc.message[:100])
        return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                await ctx.media.broadcast(session, text)
                continue
            payload = message.get("bytes")
            if payload is not None:
                await ctx.media.broadcast(session, payload)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await _upstream_finished(ctx, session)


async def _upstream_finished(ctx: Any, session: MediaSession) -> None:
    try:
        if session.upstream is not None:
            await ctx.media.upstream_closed(session)
    except Exception:  # noqa: BLE001 - 收尾不能反过来影响别的会话
        logger.exception("关闭媒体会话失败：%s", session.sid)


async def _send_loop(websocket: WebSocket, queue: Any) -> None:
    while True:
        item = await queue.pop()
        if isinstance(item, bytes):
            await websocket.send_bytes(item)
        else:
            await websocket.send_text(item)


async def _recv_loop(websocket: WebSocket) -> None:
    """观众端不发送任何业务指令；这个循环只用来发现断开。"""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_text(_json({"t": "error", "code": code, "message": message}))
