"""媒体通道：观众下行、上游上行、会话控制、快照回传。

四条路由，各自对应方案 §3.3 的一格：

    WSS /ws/live/{agent_id}       浏览器看画面（媒体）
    WSS /ws/agent/media           机器人推画面（C1）
    POST/DELETE/GET .../stream    会话控制（"开始/结束/现在怎么样"）
    POST /internal/snapshot/{id}  快照二进制回传（独立于控制通道）

鉴权刻意分两套：浏览器走会话 Cookie（`_authorize_browser`），机器人走 agent
token（`_authorize_agent`）。WebSocket 不能用 `Depends(require_session)`——
依赖里抛的是 HTTPException，在 WS 握手阶段表现成一个没有关闭码的断开，
排查时只能靠猜。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from ..deps import AgentId, AppContext, Context, SessionToken, bearer_from_header
from ..errors import ApiError
from ..media import MediaError, MediaSession, Viewer, encode_note
from ..protocol import CommandName, ErrorCode, Message, MessageType

logger = logging.getLogger("robot-monitor.routes.media")

router = APIRouter()

#: 浏览器侧断线后服务端还要等多久才认定观众走了。
VIEWER_POLL_SECONDS = 0.5
#: 建立上游的超时。相机慢的时候它可能接近这个值。
START_TIMEOUT = 12.0


def _ctx(websocket: WebSocket) -> AppContext:
    return websocket.app.state.ctx


async def _authorize_browser(websocket: WebSocket, ctx: AppContext) -> bool:
    raw = websocket.cookies.get(ctx.settings.cookie_name)
    session = await ctx.auth.verify_session(raw)
    if session is None:
        await websocket.close(code=4401, reason="未登录或会话已过期")
        return False
    return True


async def _authorize_agent(websocket: WebSocket, ctx: AppContext) -> str | None:
    row = await ctx.auth.authenticate_agent(
        bearer_from_header(websocket.headers.get("authorization"))
    )
    if row is None:
        await websocket.close(code=4401, reason="invalid or revoked agent token")
        return None
    return str(row["id"])


class StreamRequest(BaseModel):
    cameraId: str | None = Field(default=None, max_length=64)
    profile: str = Field(default="low", max_length=16)


# --- 观众通道 -----------------------------------------------------------


@router.websocket("/ws/live/{agent_id}")
async def live_socket(
    websocket: WebSocket,
    agent_id: str,
    cameraId: str | None = Query(default=None),
    profile: str = Query(default="low", max_length=16),
) -> None:
    """浏览器看画面。第一条帧永远是 `{"t":"config"}` 或 `{"t":"error"}`。

    页面在拿到 error 之前不能显示"直播中"——这就是它存在的意义。
    """
    ctx = _ctx(websocket)
    await websocket.accept()
    if not await _authorize_browser(websocket, ctx):
        return

    session, error = await _ensure_session(ctx, agent_id, cameraId, profile)
    if error is not None:
        with contextlib.suppress(Exception):
            await websocket.send_text(encode_note(error))
        await websocket.close(code=1011, reason="stream unavailable")
        return

    viewer = session.add_viewer()
    # 新观众从关键帧开始。如果上游正在低档位（可能连跳几段 GOP），
    # 不主动要一个 IDR 的话，用户会盯着黑屏等好几秒。
    await ctx.media.send_control(session, "keyframe", reason="viewer_joined")
    try:
        await _serve_viewer(websocket, session, viewer)
    finally:
        session.remove_viewer(viewer.viewer_id)
        _schedule_close(ctx, session)


async def _ensure_session(
    ctx: AppContext, agent_id: str, camera_id: str | None, profile: str
) -> tuple[MediaSession | None, dict[str, Any] | None]:
    """拿到（必要时建立）会话。返回 (会话, 错误消息)。"""
    camera = camera_id or "default"
    session = ctx.media.get(agent_id, camera)
    if session is not None and session.upstream is not None:
        ctx.media.cancel_idle_close(session)
        return session, None
    if session is None:
        session = ctx.media.create(agent_id, camera, profile)
    else:
        ctx.media.cancel_idle_close(session)

    if not ctx.hub.is_online(agent_id):
        await ctx.media.close(session, "agent_offline")
        return None, {
            "t": "error",
            "code": ErrorCode.AGENT_OFFLINE.value,
            "message": "设备离线，无法建立媒体会话",
        }

    try:
        await ctx.commands.dispatch(
            agent_id,
            CommandName.CAMERA_STREAM_START,
            # 会话键用 "default" 便于稳定，但下发给 agent 的 camera_id 保持空：
            # 空 = 本地那台球形相机，具体编号由机器人侧的相机目录决定。
            {"camera_id": camera_id or "", "profile": profile},
            session_id=session.session_id,
            deadline_seconds=START_TIMEOUT,
            timeout=START_TIMEOUT,
        )
    except ApiError as exc:
        await ctx.media.close(session, f"start_failed:{exc.code.value}")
        return None, {"t": "error", "code": exc.code.value, "message": exc.message}
    except Exception as exc:
        logger.exception("建立媒体会话失败：%s/%s", agent_id, camera)
        await ctx.media.close(session, "start_failed")
        return None, {
            "t": "error",
            "code": ErrorCode.INTERNAL.value,
            "message": str(exc),
        }

    return session, None


async def _serve_viewer(
    websocket: WebSocket, session: MediaSession, viewer: Viewer
) -> None:
    """把队列里的帧写到浏览器。文本（config/error）优先。"""

    async def writer() -> None:
        while True:
            while not viewer.notes.empty():
                await websocket.send_text(encode_note(viewer.notes.get_nowait()))
            try:
                frame = await asyncio.wait_for(viewer.queue.get(), VIEWER_POLL_SECONDS)
            except TimeoutError:
                continue
            await websocket.send_bytes(frame)

    async def reader() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    tasks = [
        asyncio.create_task(writer(), name="live-writer"),
        asyncio.create_task(reader(), name="live-reader"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            # 浏览器断开是常态（关页面/切网），不是错误。
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _schedule_close(ctx: AppContext, session: MediaSession) -> None:
    async def on_close(closed: MediaSession, reason: str) -> None:
        with contextlib.suppress(Exception):
            await ctx.commands.dispatch(
                closed.agent_id,
                CommandName.CAMERA_STREAM_STOP,
                {"session_id": closed.session_id, "camera_id": closed.camera_id},
                session_id=closed.session_id,
                deadline_seconds=5.0,
                timeout=6.0,
            )
        ctx.commands.forget_session(closed.agent_id, closed.session_id)

    ctx.media.schedule_idle_close(session, on_close)


# --- 机器人上行 ---------------------------------------------------------


@router.websocket("/ws/agent/media")
async def agent_media_socket(websocket: WebSocket) -> None:
    """机器人推画面（C1）。会话必须已经由云端创建（`camera.stream.start`）。

    这里不创建会话：凭空冒出来的 media 连接说明状态不一致，接进来只会让
    页面看到一个没有观众的幽灵流。
    """
    ctx = _ctx(websocket)
    await websocket.accept()

    agent_id = await _authorize_agent(websocket, ctx)
    if agent_id is None:
        return

    session_id = websocket.query_params.get("session_id") or ""
    try:
        session = ctx.media.attach_upstream(session_id, websocket, agent_id=agent_id)
    except MediaError as exc:
        # 不属于自己的会话用 4403，不存在用 4404：两者对排查的意义不同
        # （前者是越权/串号，后者是时序问题——会话已被回收）。
        logger.warning("拒绝媒体上游（%s）：%s", agent_id, exc.message)
        code = 4403 if exc.code == "cross_agent" else 4404
        await websocket.close(code=code, reason=exc.message[:100])
        return

    logger.info("媒体上游接入：%s session=%s", agent_id, session_id)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is not None:
                _on_upstream_text(ctx, session, text)
                continue
            data = message.get("bytes")
            if data:
                session.broadcast(bytes(data))
                if session.consume_keyframe_request():
                    # 有观众丢帧：向上游要关键帧。低档位下这一步能把黑屏从
                    # "等 stride 个 GOP"缩短到"等一个 GOP"。
                    await ctx.media.send_control(session, "keyframe", reason="viewer_reset")
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("媒体上游异常断开：%s", session_id, exc_info=True)
    finally:
        if session.upstream is websocket:
            session.upstream = None
            # 不推 `t:error`：上游掉线是暂时的，agent 会自己重连。页面靠
            # last_frame_at 的看门狗进入"卡顿/停滞"，这比一条永久错误准确。
            logger.info("媒体上游断开：%s session=%s", agent_id, session_id)
        # 对端已断时 close 会抛（Starlette 会拒绝二次关闭），这里只是收尾。
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)


def _on_upstream_text(ctx: AppContext, session: MediaSession, raw: str) -> None:
    try:
        message = Message.parse(raw)
    except Exception:  # noqa: BLE001 - 坏帧不该打断上行
        logger.debug("媒体上游发来无法解析的文本帧")
        return

    if message.type is MessageType.MEDIA_CONFIG:
        session.send_config(dict(message.payload))
        return
    if message.type is MessageType.MEDIA_STATS:
        payload = dict(message.payload)
        payload["t"] = "stats"
        for viewer in list(session.viewers.values()):
            viewer.offer_text(payload)
        return
    logger.debug("媒体上游发来未处理的类型：%s", message.type)


# --- 会话控制 -----------------------------------------------------------


@router.get("/api/v1/agents/{agent_id}/camera/stream")
async def stream_status(agent_id: str, ctx: Context, token: SessionToken) -> dict[str, Any]:
    sessions = [session.status() for session in ctx.media.for_agent(agent_id)]
    return {"items": sessions, "total": len(sessions)}


@router.post("/api/v1/agents/{agent_id}/camera/stream")
async def stream_start(
    agent_id: str, body: StreamRequest, ctx: Context, token: SessionToken
) -> dict[str, Any]:
    """主动建立会话（页面还没连 /ws/live 时用，比如先拍一张再决定要不要播）。"""
    camera = body.cameraId or "default"
    session, error = await _ensure_session(ctx, agent_id, camera, body.profile)
    if error is not None:
        try:
            code = ErrorCode(str(error["code"]))
        except ValueError:
            code = ErrorCode.INTERNAL
        raise ApiError(503, code, str(error["message"]))
    assert session is not None
    return session.status()


class ProfileRequest(BaseModel):
    profile: str = Field(default="low", max_length=16)


@router.post("/api/v1/agents/{agent_id}/camera/stream/profile")
async def stream_profile(
    agent_id: str,
    body: ProfileRequest,
    ctx: Context,
    token: SessionToken,
    cameraId: str | None = Query(default=None),
) -> dict[str, Any]:
    """切码率档位（弱网降级用）。

    只改"发多少"，不改"发什么"：按整段 GOP 保留/跳过，不做转码。
    这样机器人 CPU 成本是零，坏处是帧率只能按 GOP 的整数倍降——
    对"先保画面连续性"的目标来说足够。
    """
    if body.profile not in {"high", "low", "tiny"}:
        raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, f"未知档位：{body.profile}")
    session = ctx.media.get(agent_id, cameraId or "default")
    if session is None:
        raise ApiError(409, ErrorCode.STREAM_DISABLED, "这台设备上没有进行中的媒体会话")
    sent = await ctx.media.send_control(session, "profile", profile=body.profile)
    if not sent:
        raise ApiError(503, ErrorCode.STREAM_DISABLED, "媒体上游不在线，档位未下发")
    # 本地也记一份：上游没回执时页面仍能显示"请求已下发"，真实生效靠帧数据验证。
    session.profile = body.profile
    return {"profile": body.profile, **session.status()}


@router.delete("/api/v1/agents/{agent_id}/camera/stream")
async def stream_stop(
    agent_id: str, ctx: Context, token: SessionToken, cameraId: str | None = Query(default=None)
) -> dict[str, Any]:
    """强制结束会话（踢掉所有观众）。观众会收到 WS 关闭，页面按重连退避处理。"""
    sessions = (
        [ctx.media.get(agent_id, cameraId)] if cameraId else ctx.media.for_agent(agent_id)
    )
    closed = 0
    for session in sessions:
        if session is None:
            continue
        ctx.commands.forget_session(session.agent_id, session.session_id)
        with contextlib.suppress(Exception):
            await ctx.commands.dispatch(
                session.agent_id,
                CommandName.CAMERA_STREAM_STOP,
                {"session_id": session.session_id, "camera_id": session.camera_id},
                session_id=session.session_id,
                deadline_seconds=5.0,
                timeout=6.0,
            )
        await ctx.media.close(session, "admin_stopped")
        closed += 1
    return {"closed": closed}


# --- 快照回传 -----------------------------------------------------------


@router.post("/internal/snapshot/{request_id}")
async def upload_snapshot(request_id: str, request: Request, agent_id: AgentId) -> dict[str, Any]:
    """机器人把 JPEG 送到这里。鉴权用 agent token，与控制通道同一把。

    没人等这张图（页面关了 / 已超时）返回 404：agent 据此回一个明确的
    `expired`，而不是以为送达了。
    """
    ctx: AppContext = request.app.state.ctx
    body = await request.body()
    if not body:
        raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, "空图像")
    if not body.startswith(b"\xff\xd8"):
        raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, "不是 JPEG 数据")
    meta = {
        "captured_at": request.headers.get("x-captured-at") or "",
        "size": len(body),
        "agent_id": agent_id,
    }
    accepted = ctx.commands.deliver_snapshot(request_id, body, meta)
    if not accepted:
        raise ApiError(404, ErrorCode.EXPIRED, "没有人在等这张图（已超时或页面已关闭）")
    return {"ok": True, "size": len(body)}
