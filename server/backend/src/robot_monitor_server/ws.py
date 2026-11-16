"""`/ws/agent`：B 反向接入口（控制 C0 + 状态）。

握手顺序是刻意写死的：

1. 先校验 token（此时不 accept，但为了能给客户端一个可读的关闭原因，
   还是 accept 再 close——`websockets` 的客户端在 `connect()` 之后拿不到
   关闭码的话，排查只能靠猜）
2. 第一条帧必须是 `hello`，10s 内不发就关
3. 注册进 hub → 标记在线 → 推 SSE
4. 把未完成的上传列表塞进 welcome 帧，让 B 自己续传

第 4 步是"B 重启后能续传"的全部实现：A 不需要主动推任何东西，B 一连上就
拿到了自己该干什么。

这条连接同时承载**状态**（`state` 帧），但两者的背压完全不同：命令回包不能丢，
状态可以整帧丢。所以状态帧进来就地覆盖 `StateStore`，不进任何队列；版本落后
或字段不合法时丢弃并计数，而不是回一个错误让 B 重发——状态没有"重发"的意义。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .deps import AppContext, bearer_from_header
from .hub import AgentConnection, AgentMeta
from .protocol import (
    PROTOCOL_VERSION,
    Message,
    MessageType,
    ProtocolError,
)

logger = logging.getLogger("robot-monitor.ws")

router = APIRouter()

HELLO_TIMEOUT = 10.0
HEARTBEAT_INTERVAL = 15.0
# 心跳写库的节流：每 15s 一次心跳、每次都 UPDATE 会产生无意义的写放大
TOUCH_INTERVAL = 30.0


@router.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    ctx: AppContext = websocket.app.state.ctx

    row = await ctx.auth.authenticate_agent(
        bearer_from_header(websocket.headers.get("authorization"))
    )
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
    capabilities = _as_str_list(payload.get("capabilities"))
    media_profiles = _as_str_list(payload.get("media_profiles"))

    await ctx.db.execute(
        "UPDATE agents SET hostname = COALESCE(?, hostname), os = COALESCE(?, os),"
        " agent_version = COALESCE(?, agent_version), roots = ?,"
        " capabilities = ?, media_profiles = ? WHERE id = ?",
        (
            hostname,
            os_name,
            version,
            _json_list(roots),
            _json_list(capabilities),
            _json_list(media_profiles),
            agent_id,
        ),
    )

    meta = AgentMeta(
        id=agent_id,
        name=str(row["name"]),
        hostname=hostname,
        os=os_name,
        agent_version=version,
        roots=roots,
        capabilities=capabilities,
        media_profiles=media_profiles,
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
            elif message.type is MessageType.STATE:
                await _on_state(ctx, agent_id, message.payload)
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


async def _on_state(ctx: AppContext, agent_id: str, payload: dict[str, object]) -> None:
    """落一帧状态。

    被拒（版本落后 / 缺字段）是**正常流量**而不是错误：状态允许丢旧值，
    重连瞬间两边都很可能有几帧乱序。所以这里只 debug 一行，然后把已经
    被接受的新状态推进 SSE，让浏览器按 `state_version` 自己覆盖。
    """
    accepted = await ctx.states.put(agent_id, dict(payload))
    if not accepted:
        logger.debug("agent %s sent a stale or malformed state frame", agent_id)
        return
    record = await ctx.states.get(agent_id)
    if record is not None:
        ctx.bus.publish("state", record.view())


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_str_list(value: object, limit: int = 32) -> list[str]:
    """hello 里的能力清单：只接受字符串数组，截断到 `limit` 项。

    字段缺失或类型不对时返回空列表——对页面来说"没声明"与"没有"呈现相同，
    都不该出现远程画面按钮。截断是防御性的：能力清单不该被用来撑大帧。
    """
    if not isinstance(value, list):
        return []
    return [item for item in value[:limit] if isinstance(item, str) and item]


def _json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)

