"""媒体会话测试：一路上游、观众上限、丢帧重置、空闲回收。

这些用例全部围绕"不出现无界积压"和"上游只有一个"两条不变量，
因为它们是这类中继最容易在真实弱网下退化的地方。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from robot_monitor_server.config import Settings
from robot_monitor_server.media import (
    FLAG_KEY,
    FLAG_RESET,
    FrameQueue,
    MediaError,
    MediaHub,
)

FRAME = bytes([0x00]) + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + b"payload"
KEY_FRAME = bytes([FLAG_KEY]) + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + b"key"
CONFIG = '{"t":"config","codec":"avc1.64001F","t0":0,"size":[1280,720]}'


class FakeAgents:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, agent_id, command, payload=None, *, timeout=None):
        self.calls.append((agent_id, getattr(command, "value", str(command)), dict(payload or {})))
        return {"ok": True}


def settings(**overrides) -> Settings:
    values = {"media_idle_seconds": 0.01, "media_max_session_seconds": 60}
    values.update(overrides)
    return Settings(**values)


def test_join_starts_one_upstream_and_shares_it() -> None:
    async def scenario() -> None:
        agents = FakeAgents()
        hub = MediaHub(agents, settings(media_max_viewers=2))

        session, first = await hub.join("a1", "ptz", object())
        session2, second = await hub.join("a1", "ptz", object())

        assert session2 is session
        assert len(session.viewers) == 2
        starts = [call for call in agents.calls if call[1] == "camera.stream.start"]
        assert len(starts) == 1, "第二个观众不应该再拉起一路上游"
        assert starts[0][2]["sessionId"] == session.sid
        assert hub.status("a1", "ptz")["viewers"] == 2

        await hub.leave(session, first)
        await hub.leave(session, second)

    asyncio.run(scenario())


def test_viewer_limit_is_enforced() -> None:
    async def scenario() -> None:
        hub = MediaHub(FakeAgents(), settings(media_max_viewers=1))
        await hub.join("a1", "ptz", object())
        with pytest.raises(MediaError) as error:
            await hub.join("a1", "ptz", object())
        assert error.value.code == "camera_busy"

    asyncio.run(scenario())


def test_late_viewer_receives_cached_config() -> None:
    async def scenario() -> None:
        hub = MediaHub(FakeAgents(), settings())
        session, first = await hub.join("a1", "ptz", object())
        await hub.broadcast(session, CONFIG)
        assert session.config_text == CONFIG

        _, late = await hub.join("a1", "ptz", object())
        handshake = json.loads(await late.queue.pop())
        assert handshake["t"] == "session"
        item = await late.queue.pop()
        assert item == CONFIG, "后加入的观众必须先拿到编码参数，否则无法 configure 解码器"

        await hub.leave(session, first)
        await hub.leave(session, late)

    asyncio.run(scenario())


def test_queue_drops_deltas_and_resets_at_keyframe() -> None:
    queue = FrameQueue(max_frames=2)
    for _ in range(4):
        queue.push_video(FRAME)
    assert queue.dropped > 0
    queue.push_video(KEY_FRAME)

    items = [queue._items.popleft() for _ in range(len(queue._items))]
    assert items, "关键帧必须被保留"
    assert items[-1][0] & FLAG_RESET, "丢过帧之后，关键帧必须带 RESET 标志"


def test_idle_session_stops_upstream() -> None:
    async def scenario() -> None:
        agents = FakeAgents()
        hub = MediaHub(agents, settings(media_idle_seconds=0.02))
        session, viewer = await hub.join("a1", "ptz", object())
        await hub.leave(session, viewer)
        await asyncio.sleep(0.1)

        stops = [call for call in agents.calls if call[1] == "camera.stream.stop"]
        assert stops, "最后一个观众离开后必须结束上游"
        assert hub.status("a1", "ptz")["active"] is False

    asyncio.run(scenario())


def test_upstream_close_notifies_viewers() -> None:
    async def scenario() -> None:
        hub = MediaHub(FakeAgents(), settings(media_max_session_seconds=60))
        session, viewer = await hub.join("a1", "ptz", object())
        await hub.upstream_closed(session)

        # 队列里先是会话信息，然后才是上游结束的 stop。
        messages = []
        for _ in range(2):
            try:
                messages.append(json.loads(await asyncio.wait_for(viewer.queue.pop(), 0.5)))
            except TimeoutError:
                break
        assert messages[-1]["t"] == "stop"
        assert hub.status("a1", "ptz")["active"] is False

    asyncio.run(scenario())


def test_join_failure_removes_phantom_viewer() -> None:
    """建流失败时观众必须被摘掉，否则人数永远 >0，空闲回收不会触发。"""

    class FailingAgents:
        async def request(self, *_args, **_kwargs):
            raise RuntimeError("agent offline")

    async def scenario() -> None:
        hub = MediaHub(FailingAgents(), settings())
        with pytest.raises(MediaError) as error:
            await hub.join("a1", "ptz", object())
        assert error.value.code == "agent_offline"
        assert hub.status("a1", "ptz")["active"] is False

    asyncio.run(scenario())


def test_close_is_idempotent() -> None:
    """stop 之后观众再走 leave，不能再给 agent 发一次 stop。"""

    async def scenario() -> None:
        agents = FakeAgents()
        hub = MediaHub(agents, settings())
        session, viewer = await hub.join("a1", "ptz", object())
        await hub.stop("a1", "ptz")
        await hub.leave(session, viewer)
        await asyncio.sleep(0.05)

        stops = [call for call in agents.calls if call[1] == "camera.stream.stop"]
        assert len(stops) == 1

    asyncio.run(scenario())
