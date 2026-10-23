"""`/ws/agent`：B 反向接入口。

握手顺序是刻意写死的：

1. 先校验 token（此时不 accept，但为了能给客户端一个可读的关闭原因，
   还是 accept 再 close——`websockets` 的客户端在 `connect()` 之后拿不到
   关闭码的话，排查只能靠猜）
2. 第一条帧必须是 `hello`，10s 内不发就关
3. 注册进 hub → 标记在线 → 推 SSE
4. 把未完成的上传列表塞进 welcome 帧，让 B 自己续传

第 4 步是"B 重启后能续传"的全部实现：A 不需要主动推任何东西，B 一连上就
拿到了自己该干什么。
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import AppContext
from .hub import AgentConnection, AgentMeta
from .protocol import (
    PROTOCOL_VERSION,
    Message,
    MessageType,
    ProtocolError,
)

logger = logging.getLogger("filebridge.ws")

router = APIRouter()

HELLO_TIMEOUT = 10.0
HEARTBEAT_INTERVAL = 15.0
# 心跳写库的节流：每 15s 一次心跳、每次都 UPDATE 会产生无意义的写放大
TOUCH_INTERVAL = 30.0


@router.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx

    row = await ctx.auth.authenticate_agent(bearer_token_from_ws(websocket))
    await websocket.accept()

    if row is None:
        await websocket.close(code=4401, reason="invalid or revoked agent token")
        return

    agent_id = str(row["id"])
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT)
    except TimeoutError:
        await websocket.close(code=4408, reason="hello frame timeout")
        return
    except WebSocketDisconnect:
        return

    try:
        hello = Message.parse(raw)
    except ProtocolError as exc:
        await websocket.close(code=4400, reason=str(exc)[:120])
        return

    if hello.type is not MessageType.HELLO:
        await websocket.close(code=4400, reason="first frame must be hello")
        return

    payload = hello.payload
    hostname = _as_str(payload.get("hostname"))
    os_name = _as_str(payload.get("os"))
    version = _as_str(payload.get("agent_version"))
    roots = [str(r) for r in payload.get("roots", []) if isinstance(r, str)]

    await ctx.db.execute(
        "UPDATE agents SET hostname = COALESCE(?, hostname), os = COALESCE(?, os),"
        " agent_version = COALESCE(?, agent_version), roots = ? WHERE id = ?",
        (hostname, os_name, version, _json_list(roots), agent_id),
    )

    meta = AgentMeta(
        id=agent_id,
        name=str(row["name"]),
        hostname=hostname,
        os=os_name,
        agent_version=version,
        roots=roots,
    )
    conn = AgentConnection(
        websocket=websocket,
        meta=meta,
        command_timeout=ctx.settings.command_timeout_seconds,
    )
    conn.start()
    await ctx.hub.register(conn)

    # 把该续传的上传一并交给 B。这一步失败不应该影响连接本身。
    resume: list[dict[str, object]] = []
    try:
        resume = await ctx.tasks.pending_uploads(agent_id)
    except Exception:  # pragma: no cover - 数据库异常时仍然允许连接可用
        logger.exception("failed to collect pending uploads for %s", agent_id)

    welcome = Message(
        type=MessageType.ACK,
        payload={
            "server_time": time.time(),
            "protocol": PROTOCOL_VERSION,
            "heartbeat_interval": HEARTBEAT_INTERVAL,
            "resume_uploads": resume,
            "agent_id": agent_id,
        },
    )
    try:
        await conn.enqueue(welcome.to_json())
    except Exception:
        logger.exception("failed to send welcome to %s", agent_id)

    last_touch = time.time()
    try:
        while True:
            raw = await websocket.receive_text()
            conn.last_seen = time.time()

            try:
                message = Message.parse(raw)
            except ProtocolError as exc:
                logger.warning("agent %s sent an invalid frame: %s", agent_id, exc)
                await websocket.close(code=4400, reason=str(exc)[:120])
                break

            if message.type is MessageType.RESULT:
                if not conn.resolve(message):
                    logger.debug("agent %s returned an unmatched result %s", agent_id, message.id)
            elif message.type is MessageType.EVENT:
                try:
                    await ctx.tasks.handle_agent_event(agent_id, message.payload)
                except Exception:
                    logger.exception("failed to handle agent event from %s", agent_id)
            elif message.type is MessageType.ERROR:
                detail = message.error
                logger.warning(
                    "agent %s reported error %s: %s",
                    agent_id,
                    detail.code.value if detail else "?",
                    detail.message if detail else "",
                )
            else:
                logger.debug("agent %s sent unhandled type %s", agent_id, message.type)

            if time.time() - last_touch > TOUCH_INTERVAL:
                last_touch = time.time()
                await ctx.auth.touch_agent(agent_id)

    except WebSocketDisconnect:
        logger.info("agent %s disconnected", agent_id)
    except Exception:
        logger.exception("agent %s connection crashed", agent_id)
    finally:
        await conn.shutdown("connection closed", code=1000)
        await ctx.hub.unregister(conn)


def bearer_token_from_ws(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_list(values: list[str]) -> str:
    import json

    return json.dumps(values, ensure_ascii=False)

