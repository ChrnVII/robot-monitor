"""媒体会话与扇出：一路上游，多个观众。

三条规则（方案 §4.3），每条都对应一类真实故障：

1. **会话按 (agent_id, camera_id) 唯一。** 第一个观众触发 `camera.stream.start`，
   最后一个观众离开后延迟 10s 关闭——否则用户每刷新一次页面就重建一次 RTSP，
   相机侧会看到一串"连上又断开"。
2. **上游只发一次，云端扇出。** 机器人出口带宽与观众数无关。观众从 1 变 5
   只是多几个 Queue，不是多几条 RTSP。
3. **每个观众一个有界队列，满了丢最旧的，并置 need_keyframe。**
   丢帧之后那个观众必须从关键帧重新起步，否则后续 P 帧全部花屏。
   这与 `events.py` 的"每订阅者独立队列、满了丢、由其自行恢复"是同一套思路，
   只是恢复手段从"REST 对账"换成了"等关键帧"。

`last_frame_at` 是**判定画面是否新鲜的唯一依据**——不是"连接是否 open"。
连接开着但十秒没帧，页面必须显示停滞，而不是"直播中"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket

from .protocol import MEDIA_FLAG_KEY, MEDIA_FLAG_RESET, MEDIA_HEADER_BYTES

logger = logging.getLogger("robot-monitor.media")

#: 每观众队列上限（帧）。8 帧 ≈ 1s 的缓冲，再多就不是"实时"了。
VIEWER_QUEUE = 8
#: 观众侧文本消息（config/error/stats）的槽位。频率低，但一条都不能丢。
VIEWER_NOTES = 4
#: 最后一个观众离开后的保留时间：刷新页面不该重建流。
IDLE_CLOSE_SECONDS = 10.0
#: 上游握手超时：`camera.stream.start` 之后多久没连上来就算失败。
UPSTREAM_TIMEOUT = 10.0


class MediaError(Exception):
    """会话建立/维持失败。code 直接给到页面（`{"t":"error"}`）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class Viewer:
    """一个浏览器观众：一条有界帧队列 + 一条文本槽。"""

    __slots__ = ("dropped", "joined_at", "need_keyframe", "notes", "queue", "viewer_id")

    def __init__(self, viewer_id: str) -> None:
        self.viewer_id = viewer_id
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(VIEWER_QUEUE)
        self.notes: asyncio.Queue[dict[str, Any]] = asyncio.Queue(VIEWER_NOTES)
        # 新观众从关键帧开始：它没看到历史帧，任何 P 帧都是花屏。
        self.need_keyframe = True
        self.dropped = 0
        self.joined_at = time.time()

    def offer(self, frame: bytes) -> None:
        """投递一帧。永不阻塞：满了就丢最旧的。

        丢过帧之后只放行关键帧，并给它打上 RESET 位——浏览器据此重建解码器。
        这比"继续喂 P 帧"好得多：P 帧缺参考只会花屏，而且没人知道什么时候恢复。

        `dropped` 计的是"这个观众没拿到的帧数"，所以被顶掉的和之后被拒的都算，
        两者加起来刚好等于总帧数减已入队数——指标能对得上，不是估的。
        """
        if len(frame) <= MEDIA_HEADER_BYTES:
            return
        keyframe = bool(frame[0] & MEDIA_FLAG_KEY)

        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()      # 丢最旧的一帧
            self.dropped += 1
            self.need_keyframe = True

        if self.need_keyframe:
            if not keyframe:
                self.dropped += 1            # 缺参考的 P 帧：收了也没用
                return
            frame = bytes([frame[0] | MEDIA_FLAG_RESET]) + frame[1:]
            self.need_keyframe = False

        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(frame)

    def offer_text(self, message: dict[str, Any]) -> None:
        """config/error/stats 这类文本消息。满了丢最旧的——只会丢掉过期的统计。"""
        if self.notes.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.notes.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.notes.put_nowait(message)


def _message_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class MediaSession:
    """一路相机的一次会话：一个上游 + 若干观众。"""

    def __init__(self, agent_id: str, camera_id: str, profile: str = "low") -> None:
        self.agent_id = agent_id
        self.camera_id = camera_id
        self.session_id = _message_id("med")
        self.profile = profile
        self.viewers: dict[str, Viewer] = {}
        self.upstream: WebSocket | None = None
        self.config: dict[str, Any] | None = None
        self.started_at = time.time()
        self.rx_bytes = 0
        self.frames = 0
        self.dropped_frames = 0
        self.keyframe_waits = 0
        self.last_frame_at: float | None = None
        self.idle_task: asyncio.Task[None] | None = None
        #: 有观众因丢帧需要重新起步：下一次广播后向上游要一个关键帧。
        self.want_keyframe = False

    # --- 状态 -----------------------------------------------------------

    @property
    def viewer_count(self) -> int:
        return len(self.viewers)

    def freshness_ms(self, now: float | None = None) -> int | None:
        if self.last_frame_at is None:
            return None
        moment = now if now is not None else time.time()
        return round(max(0.0, moment - self.last_frame_at) * 1000)

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "camera_id": self.camera_id,
            "profile": self.profile,
            "upstream": "connected" if self.upstream is not None else "waiting",
            "viewers": self.viewer_count,
            "codec": (self.config or {}).get("codec"),
            "size": (self.config or {}).get("size"),
            "frames": self.frames,
            "rx_bytes": self.rx_bytes,
            "dropped_frames": self.dropped_frames,
            "keyframe_waits": self.keyframe_waits,
            "want_keyframe": self.want_keyframe,
            "last_frame_at": self.last_frame_at,
            # 页面判断"画面是否真的新鲜"用这个，不用连接状态。
            "media_last_frame_age_ms": self.freshness_ms(),
            "started_at": self.started_at,
        }

    # --- 观众 -----------------------------------------------------------

    def add_viewer(self) -> Viewer:
        viewer = Viewer(_message_id("v"))
        self.viewers[viewer.viewer_id] = viewer
        if self.config:
            # 已经在跑：立刻把 config 给新观众，它不用等下一次 config 事件。
            viewer.offer_text(self._config_message())
        return viewer

    def remove_viewer(self, viewer_id: str) -> None:
        self.viewers.pop(viewer_id, None)

    def broadcast(self, frame: bytes) -> None:
        self.frames += 1
        self.rx_bytes += len(frame)
        self.last_frame_at = time.time()
        if frame[0] & MEDIA_FLAG_KEY:
            self.keyframe_waits = 0
        dropped_before = sum(viewer.dropped for viewer in self.viewers.values())
        for viewer in list(self.viewers.values()):
            viewer.offer(frame)
        if sum(viewer.dropped for viewer in self.viewers.values()) > dropped_before:
            # 有人丢帧了：请求上游尽快给一个 IDR，别让那个观众一直等 GOP。
            # 真正等多久由 `keyframe_waits` 记账（从丢帧到下一个关键帧的帧数）。
            self.want_keyframe = True
            self.keyframe_waits += 1

    def consume_keyframe_request(self) -> bool:
        wanted, self.want_keyframe = self.want_keyframe, False
        return wanted

    def send_config(self, config: dict[str, Any]) -> None:
        """config 到了/变了：推给所有观众。浏览器收到后会重建解码器。"""
        self.config = config
        for viewer in list(self.viewers.values()):
            viewer.offer_text(self._config_message())

    def _config_message(self) -> dict[str, Any]:
        config = self.config or {}
        return {
            "t": "config",
            "codec": config.get("codec"),
            "t0": config.get("t0"),
            "size": config.get("size") or [0, 0],
            "profile": config.get("profile") or self.profile,
        }

    def send_error(self, code: str, message: str) -> None:
        payload = {"t": "error", "code": code, "message": message}
        for viewer in list(self.viewers.values()):
            viewer.offer_text(payload)


class MediaRegistry:
    """会话表 + 会话生命周期。

    进程内字典：单 worker（见 `main.py`）。接口留在这里，将来换 Redis 时
    业务代码（路由、命令服务）不跟着改。
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], MediaSession] = {}
        self._by_id: dict[str, MediaSession] = {}

    # --- 查询 -----------------------------------------------------------

    def get(self, agent_id: str, camera_id: str) -> MediaSession | None:
        return self._sessions.get((agent_id, camera_id))

    def get_by_session_id(self, session_id: str) -> MediaSession | None:
        return self._by_id.get(session_id)

    def for_agent(self, agent_id: str) -> list[MediaSession]:
        return [s for (aid, _), s in self._sessions.items() if aid == agent_id]

    def snapshot(self) -> list[dict[str, Any]]:
        return [session.status() for session in self._sessions.values()]

    # --- 生命周期 -------------------------------------------------------

    def create(self, agent_id: str, camera_id: str, profile: str = "low") -> MediaSession:
        existing = self.get(agent_id, camera_id)
        if existing is not None:
            return existing
        session = MediaSession(agent_id, camera_id, profile)
        self._sessions[(agent_id, camera_id)] = session
        self._by_id[session.session_id] = session
        logger.info(
            "媒体会话建立：%s/%s session=%s", agent_id, camera_id, session.session_id
        )
        return session

    def attach_upstream(self, session_id: str, websocket: WebSocket) -> MediaSession:
        session = self._by_id.get(session_id)
        if session is None:
            raise MediaError("unknown_session", f"没有这个媒体会话：{session_id}")
        previous, session.upstream = session.upstream, websocket
        if previous is not None and previous is not websocket:
            logger.info("会话 %s 的上游被新连接替换", session_id)
        self.cancel_idle_close(session)
        return session

    def cancel_idle_close(self, session: MediaSession) -> None:
        task, session.idle_task = session.idle_task, None
        if task is not None and not task.done():
            task.cancel()

    def schedule_idle_close(
        self, session: MediaSession, on_close: Any, delay: float = IDLE_CLOSE_SECONDS
    ) -> None:
        """最后一个观众走了：延迟关闭。刷新页面不会重建流。"""
        if session.viewer_count:
            return
        self.cancel_idle_close(session)

        async def closer() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if session.viewer_count:
                return
            await self.close(session, "idle", on_close)

        session.idle_task = asyncio.create_task(closer(), name=f"media-idle-{session.session_id}")

    async def close(self, session: MediaSession, reason: str, on_close: Any = None) -> None:
        key = (session.agent_id, session.camera_id)
        if self._sessions.get(key) is not session:
            return
        self._sessions.pop(key, None)
        self._by_id.pop(session.session_id, None)
        self.cancel_idle_close(session)
        upstream, session.upstream = session.upstream, None
        if upstream is not None:
            with contextlib.suppress(Exception):
                await upstream.close(code=1000, reason=reason[:80])
        logger.info(
            "媒体会话结束：%s/%s（%s）共 %d 帧", session.agent_id, session.camera_id, reason, session.frames
        )
        if on_close is not None:
            with contextlib.suppress(Exception):
                await on_close(session, reason)

    async def close_all(self, reason: str = "shutdown") -> None:
        for session in list(self._sessions.values()):
            await self.close(session, reason)

    async def send_control(self, session: MediaSession, action: str, **extra: Any) -> bool:
        """给上游发一条媒体控制帧（请求关键帧 / 终止）。"""
        upstream = session.upstream
        if upstream is None:
            return False
        from .protocol import Message, MessageType  # 局部导入：避免循环依赖

        frame = Message(
            type=MessageType.MEDIA_CONTROL,
            payload={"session_id": session.session_id, "action": action, **extra},
        )
        try:
            await upstream.send_text(frame.to_json())
        except Exception:  # noqa: BLE001 - 上游正好断掉是常态
            return False
        return True


def encode_note(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))
