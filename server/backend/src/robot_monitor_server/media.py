"""媒体会话：一路上游、多个观众、有界队列。

三条规则决定了这里的全部结构：

1. **一路上游。** 同一个 (agent_id, cameraId) 只有一条来自 cloud-agent 的媒体
   连接，无论有多少个浏览器在看。观众扇出在云端完成，机器人侧带宽不变。
2. **不允许无界积压。** 每个观众一个 8 帧的有界队列；满了就丢视频帧，
   并在下一个关键帧打 `RESET`。不这么做，弱网下表现不是"卡"，而是
   "越看越延迟"——这是最难被用户描述清楚、也最难复现的故障。
3. **会话是内存态。** 进程重启即断，浏览器自动重连时会重新拉起上游。
   不落库，因为实时画面没有"历史"可言；审计另走 audit_log。

媒体帧不解析：`flags(1)+pts(4)+seq(4)+Annex-B` 原样进、原样出。云端唯一的
改动是给丢过帧的观众补一个 `RESET` 标志位。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .protocol import CommandName, ErrorCode

logger = logging.getLogger("robot-monitor.media")

FRAME_HEADER_LENGTH = 9
FLAG_KEY = 0x01
FLAG_CONFIG = 0x02
FLAG_RESET = 0x04

#: 单观众队列上限（帧）。与机器人 backend 对浏览器订阅者的默认值一致。
VIEWER_QUEUE_FRAMES = 8


class MediaError(Exception):
    """带错误码的媒体失败，路由据此决定 HTTP 状态或 WS 关闭原因。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class FrameQueue:
    """观众的有界队列：视频可丢，JSON 不可丢。"""

    def __init__(self, max_frames: int = VIEWER_QUEUE_FRAMES) -> None:
        self._items: deque[str | bytes] = deque()
        self._max = max(1, max_frames)
        self._event = asyncio.Event()
        self.need_reset = False
        self.dropped = 0
        self.frames = 0
        self.bytes = 0

    def push_text(self, text: str) -> None:
        while len(self._items) >= self._max:
            if not self._evict_video():
                break
        self._items.append(text)
        self._event.set()

    def push_video(self, frame: bytes) -> None:
        flags = frame[0] if frame else 0
        is_key = bool(flags & FLAG_KEY)
        if self.need_reset and not is_key:
            self.dropped += 1
            return
        if len(self._items) >= self._max:
            self.dropped += 1
            self.need_reset = True
            if not is_key:
                return
        if is_key and self.need_reset:
            frame = bytes([flags | FLAG_RESET]) + frame[1:]
            self.need_reset = False
        self.frames += 1
        self.bytes += len(frame)
        self._items.append(frame)
        self._event.set()

    def _evict_video(self) -> bool:
        for index, item in enumerate(self._items):
            if isinstance(item, bytes):
                del self._items[index]
                self.need_reset = True
                self.dropped += 1
                return True
        return False

    async def pop(self) -> str | bytes:
        while not self._items:
            self._event.clear()
            await self._event.wait()
        return self._items.popleft()

    def wake(self) -> None:
        self._event.set()


@dataclass(eq=False)
class Viewer:
    ws: Any
    queue: FrameQueue = field(default_factory=FrameQueue)
    joined_at: float = field(default_factory=time.time)


@dataclass(eq=False)
class MediaSession:
    agent_id: str
    camera_id: str
    sid: str = field(default_factory=lambda: f"s-{secrets.token_hex(4)}")
    created_at: float = field(default_factory=time.time)
    viewers: set[Viewer] = field(default_factory=set)
    upstream: Any | None = None
    config_text: str | None = None
    state: str = "starting"
    #: 已经向 agent 发过 start 的时间。上游是异步接进来的，这中间又来了观众，
    #: 不能重复下发——那会让机器人建两条上游，第二条把第一条顶掉。
    requested_at: float | None = None
    frames: int = 0
    bytes: int = 0
    dropped: int = 0
    last_error: str | None = None
    idle_task: asyncio.Task[None] | None = None
    expiry_task: asyncio.Task[None] | None = None


class MediaHub:
    def __init__(self, agents: Any, settings: Settings) -> None:
        self._agents = agents
        self._settings = settings
        self._sessions: dict[tuple[str, str], MediaSession] = {}
        self._lock = asyncio.Lock()

    # --- 观众 -----------------------------------------------------------

    async def join(self, agent_id: str, camera_id: str, ws: Any) -> tuple[MediaSession, Viewer]:
        """浏览器加入。必要时向 agent 下发 start，等它把上游接进来。"""
        async with self._lock:
            key = (agent_id, camera_id)
            session = self._sessions.get(key)
            if session is None:
                session = MediaSession(agent_id=agent_id, camera_id=camera_id)
                self._sessions[key] = session
            if len(session.viewers) >= self._settings.media_max_viewers:
                raise MediaError("camera_busy", f"同时观看人数已达上限（{self._settings.media_max_viewers}）")
            viewer = Viewer(ws=ws)
            session.viewers.add(viewer)
            self._cancel(session.idle_task)
            session.idle_task = None
            if session.expiry_task is None:
                session.expiry_task = asyncio.create_task(
                    self._expire_later(session), name=f"media-expire-{session.sid}"
                )

        await self._ensure_upstream(session)
        # 先把会话信息与缓存的编码参数塞进队列：后加入的观众不必等下一帧
        # 配置（那是流刚开始时才会发一次的），否则解码器永远 configure 不上。
        viewer.queue.push_text(
            _json({
                "t": "session",
                "id": session.sid,
                "cameraId": session.camera_id,
                "state": session.state,
                "viewers": len(session.viewers),
            })
        )
        if session.config_text:
            viewer.queue.push_text(session.config_text)
        return session, viewer

    async def leave(self, session: MediaSession, viewer: Viewer) -> None:
        session.viewers.discard(viewer)
        viewer.queue.wake()
        if session.viewers:
            return
        # 最后一个观众走了：先等一小会儿。刷新页面会瞬间断开再连上，
        # 立刻拆上游会让"刷新一次卡三秒"变成常态。
        self._cancel(session.idle_task)
        session.idle_task = asyncio.create_task(
            self._idle_close(session), name=f"media-idle-{session.sid}"
        )

    async def _idle_close(self, session: MediaSession) -> None:
        try:
            await asyncio.sleep(self._settings.media_idle_seconds)
        except asyncio.CancelledError:
            return
        if session.viewers:
            return
        logger.info("媒体会话 %s 空闲 %.0fs，关闭上游", session.sid, self._settings.media_idle_seconds)
        await self._close(session, notify_viewers=False)

    async def _expire_later(self, session: MediaSession) -> None:
        try:
            await asyncio.sleep(self._settings.media_max_session_seconds)
        except asyncio.CancelledError:
            return
        logger.info("媒体会话 %s 达到最长时长，主动结束", session.sid)
        await self._close(session, notify_viewers=True, reason="session_expired")

    # --- 上游 -----------------------------------------------------------

    async def _ensure_upstream(self, session: MediaSession) -> None:
        if session.upstream is not None:
            return
        if (
            session.requested_at is not None
            and time.time() - session.requested_at < self._settings.media_command_timeout
        ):
            return
        session.requested_at = time.time()
        try:
            await self._agents.request(
                session.agent_id,
                CommandName.CAMERA_STREAM_START,
                {"cameraId": session.camera_id, "sessionId": session.sid},
                timeout=self._settings.media_command_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻译成页面能看懂的码
            session.requested_at = None
            code = getattr(exc, "code", None)
            value = getattr(code, "value", None) or ErrorCode.AGENT_OFFLINE.value
            raise MediaError(value, str(getattr(exc, "message", exc))) from exc

    async def attach_upstream(
        self, agent_id: str, camera_id: str, session_id: str, ws: Any
    ) -> MediaSession:
        session = self._sessions.get((agent_id, camera_id))
        if session is None or session.sid != session_id:
            raise MediaError("session_unknown", "媒体会话不存在或已结束")
        if session.upstream is not None:
            # 同一会话只允许一条上游；旧的多半是网络半死留下的孤儿。
            with contextlib.suppress(Exception):
                await session.upstream.close(code=1012, reason="replaced")
        session.upstream = ws
        session.state = "live"
        for viewer in list(session.viewers):
            viewer.queue.push_text(
                _json({"t": "upstream", "state": "live", "sessionId": session.sid})
            )
        return session

    def detach_upstream(self, session: MediaSession) -> None:
        session.upstream = None
        session.state = "closed"

    async def upstream_closed(self, session: MediaSession) -> None:
        """上游自己断了（机器人重启、backend 停掉、网络断）。

        观看端不能就这么黑着：发一条 stop，让前端走它自己的退避重连；
        会话移除，下一次有人看会重新拉起上游。
        """
        self._sessions.pop((session.agent_id, session.camera_id), None)
        self.detach_upstream(session)
        self._cancel(session.expiry_task)
        session.expiry_task = None
        message = _json({"t": "stop", "code": 1012, "reason": "upstream_closed"})
        for viewer in list(session.viewers):
            viewer.queue.push_text(message)
            viewer.queue.wake()
        session.viewers.clear()

    async def broadcast(self, session: MediaSession, item: str | bytes) -> None:
        if isinstance(item, str):
            session.config_text = item if '"config"' in item else session.config_text
        else:
            session.frames += 1
            session.bytes += len(item)
        for viewer in list(session.viewers):
            if isinstance(item, str):
                viewer.queue.push_text(item)
            else:
                viewer.queue.push_video(item)

    # --- 状态与关闭 -----------------------------------------------------

    def status(self, agent_id: str, camera_id: str) -> dict[str, Any]:
        session = self._sessions.get((agent_id, camera_id))
        if session is None:
            return {"active": False, "state": "idle", "viewers": 0}
        elapsed = max(0.001, time.time() - session.created_at)
        return {
            "active": True,
            "state": session.state,
            "sessionId": session.sid,
            "cameraId": session.camera_id,
            "viewers": len(session.viewers),
            "since": session.created_at,
            "fps": round(session.frames / elapsed, 1),
            "kbps": round(session.bytes * 8 / elapsed / 1000, 1),
            "dropped": sum(viewer.queue.dropped for viewer in session.viewers),
            "lastError": session.last_error,
        }

    async def stop(self, agent_id: str, camera_id: str) -> dict[str, Any]:
        session = self._sessions.get((agent_id, camera_id))
        if session is None:
            return {"stopped": False}
        await self._close(session, notify_viewers=True, reason="stopped_by_user")
        return {"stopped": True}

    async def _close(self, session: MediaSession, *, notify_viewers: bool, reason: str = "closed") -> None:
        self._sessions.pop((session.agent_id, session.camera_id), None)
        self._cancel(session.expiry_task)
        session.expiry_task = None
        upstream, session.upstream = session.upstream, None
        if notify_viewers:
            message = _json({"t": "stop", "code": 1012, "reason": reason})
            for viewer in list(session.viewers):
                viewer.queue.push_text(message)
                viewer.queue.wake()
        session.viewers.clear()
        if upstream is not None:
            with contextlib.suppress(Exception):
                await upstream.close(code=1012, reason=reason)
        with contextlib.suppress(Exception):
            await self._agents.request(
                session.agent_id,
                CommandName.CAMERA_STREAM_STOP,
                {"sessionId": session.sid},
                timeout=3.0,
            )

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await self._close(session, notify_viewers=True, reason="server_shutdown")

    # --- 小工具 ---------------------------------------------------------

    @staticmethod
    def _cancel(task: asyncio.Task[None] | None) -> None:
        if task is not None and not task.done():
            task.cancel()


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
