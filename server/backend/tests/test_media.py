"""媒体扇出与命令服务：两个最容易"看起来对、实际错"的地方。

- 观众队列：丢帧必须丢最旧的、并且从关键帧重新起步。错的写法（丢最新、或
  继续喂 P 帧）在本地网看不出问题，一到弱网就是花屏且永不恢复。
- 命令服务：超时不是失败，是 `unknown`。这条要落库，页面才有依据说"结果未知"。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from robot_monitor_server.commands import CommandService, CommandStatus
from robot_monitor_server.db import Database
from robot_monitor_server.errors import ApiError
from robot_monitor_server.hub import AgentError
from robot_monitor_server.media import MediaRegistry, MediaSession, Viewer
from robot_monitor_server.protocol import (
    MEDIA_FLAG_KEY,
    MEDIA_FLAG_RESET,
    CommandName,
    ErrorCode,
    Message,
    MessageType,
)


def frame(seq: int, *, keyframe: bool = False) -> bytes:
    """9 字节帧头 + 一点载荷。flags 位与真实链路一致。"""
    flags = MEDIA_FLAG_KEY if keyframe else 0x00
    return bytes([flags]) + seq.to_bytes(4, "little") + b"\x00\x00\x00\x00" + b"payload"


# --- 观众队列 -----------------------------------------------------------


async def test_viewer_starts_at_a_keyframe():
    """新观众没看到历史帧：P 帧对它毫无意义，还会花屏。"""
    viewer = Viewer("v1")

    viewer.offer(frame(1, keyframe=False))
    assert viewer.queue.empty()
    assert viewer.dropped == 1

    viewer.offer(frame(2, keyframe=True))
    got = viewer.queue.get_nowait()
    assert got[0] & MEDIA_FLAG_KEY
    # 第一次给关键帧时要打 RESET：浏览器据此重建解码器
    assert got[0] & MEDIA_FLAG_RESET


async def test_viewer_drops_oldest_when_full():
    viewer = Viewer("v1")
    viewer.need_keyframe = False
    for index in range(50):
        viewer.offer(frame(index))

    remaining = []
    while not viewer.queue.empty():
        remaining.append(int.from_bytes(viewer.queue.get_nowait()[1:5], "little"))

    # 溢出那一刻丢掉最旧的一帧（0），并把观众置为"等关键帧"：
    # 此后的 P 帧全部丢弃，直到下一个 IDR。这不是"丢最新"，是"丢链"。
    assert remaining == list(range(1, len(remaining) + 1))
    assert viewer.dropped == 50 - len(remaining)
    assert viewer.need_keyframe is True


async def test_viewer_after_overflow_waits_for_keyframe():
    viewer = Viewer("v1")
    viewer.need_keyframe = False
    for index in range(20):
        viewer.offer(frame(index))

    # 溢出之后再来的 P 帧一律丢弃
    before = viewer.dropped
    viewer.offer(frame(100, keyframe=False))
    assert viewer.dropped == before + 1

    viewer.offer(frame(101, keyframe=True))
    frames = []
    while not viewer.queue.empty():
        frames.append(viewer.queue.get_nowait())
    assert frames[-1][0] & MEDIA_FLAG_RESET


async def test_viewer_text_notes_are_bounded():
    viewer = Viewer("v1")
    for index in range(20):
        viewer.offer_text({"t": "stats", "i": index})

    assert viewer.notes.qsize() <= 4


# --- 会话 ---------------------------------------------------------------


async def test_registry_keeps_one_session_per_camera():
    registry = MediaRegistry()

    first = registry.create("b-1", "VAR-1")
    again = registry.create("b-1", "VAR-1")

    assert first is again
    assert registry.get("b-1", "VAR-1") is first
    assert registry.get("b-1", "other") is None


async def test_session_broadcast_updates_freshness():
    session = MediaSession("b-1", "VAR-1")
    viewer = session.add_viewer()
    viewer.need_keyframe = False

    before = session.freshness_ms()
    session.broadcast(frame(1))

    assert session.frames == 1
    assert session.last_frame_at is not None
    assert before is None
    assert session.freshness_ms() is not None


async def test_new_viewer_gets_config_immediately():
    session = MediaSession("b-1", "VAR-1")
    session.send_config({"codec": "avc1.64001F", "t0": 1, "size": [640, 360], "profile": "low"})

    viewer = session.add_viewer()

    note = viewer.notes.get_nowait()
    assert note["t"] == "config"
    assert note["codec"] == "avc1.64001F"
    assert note["profile"] == "low"


async def test_idle_close_fires_after_last_viewer_leaves():
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")
    viewer = session.add_viewer()
    session.remove_viewer(viewer.viewer_id)

    closed: list[str] = []

    async def on_close(_session, reason):
        closed.append(reason)

    registry.schedule_idle_close(session, on_close, delay=0.05)
    await asyncio.sleep(0.15)

    assert closed == ["idle"]
    assert registry.get("b-1", "VAR-1") is None


async def test_idle_close_is_cancelled_by_a_new_viewer():
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")

    closed: list[str] = []

    async def on_close(_session, reason):
        closed.append(reason)

    registry.schedule_idle_close(session, on_close, delay=0.1)
    session.add_viewer()                      # 用户刷新页面
    registry.cancel_idle_close(session)
    await asyncio.sleep(0.2)

    assert closed == []
    assert registry.get("b-1", "VAR-1") is session


async def test_viewer_drop_requests_a_keyframe():
    """有人丢帧就向上游要 IDR：低档位下能把黑屏从"等几段 GOP"缩到"等一段"。"""
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")
    viewer = session.add_viewer()
    viewer.need_keyframe = False

    for index in range(30):        # 打爆队列
        session.broadcast(frame(index))

    assert session.want_keyframe is True
    assert session.keyframe_waits >= 1
    # 取一次就清掉：同一次丢帧不该反复请求
    assert session.consume_keyframe_request() is True
    assert session.consume_keyframe_request() is False


async def test_keyframe_arrival_resets_the_counter():
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")
    session.add_viewer().need_keyframe = False

    for index in range(30):
        session.broadcast(frame(index))
    assert session.keyframe_waits >= 1

    session.broadcast(frame(100, keyframe=True))
    assert session.keyframe_waits == 0


class FakeUpstream:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, raw: str) -> None:
        self.sent.append(raw)


async def test_send_control_reaches_the_upstream():
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")
    upstream = FakeUpstream()
    session.upstream = upstream  # type: ignore[assignment]

    assert await registry.send_control(session, "profile", profile="tiny") is True

    message = Message.parse(upstream.sent[0])
    assert message.type is MessageType.MEDIA_CONTROL
    assert message.payload["action"] == "profile"
    assert message.payload["profile"] == "tiny"


async def test_send_control_without_upstream_is_false():
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")

    assert await registry.send_control(session, "keyframe") is False


async def test_upstream_from_another_agent_is_refused_without_side_effects():
    """越权接入必须在校验阶段就被挡住，不能"先挂上再关掉"。

    先挂上再检查，等于给任何一台已注册设备一个 DDoS 开关：随便报一个别人的
    session_id，那条会话就会被云端关掉（连它的观众一起）。
    """
    from robot_monitor_server.media import MediaError

    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")
    owner_upstream = object()
    session.upstream = owner_upstream  # type: ignore[assignment]

    with pytest.raises(MediaError) as raised:
        registry.attach_upstream("ignored", object(), agent_id="b-2")  # type: ignore[arg-type]

    assert raised.value.code == "unknown_session"      # 先查会话存在
    with pytest.raises(MediaError) as raised:
        registry.attach_upstream(session.session_id, object(), agent_id="b-2")  # type: ignore[arg-type]
    assert raised.value.code == "cross_agent"

    # 原会话毫发无损：上游没被替换、也没被关闭
    assert session.upstream is owner_upstream
    assert registry.get("b-1", "VAR-1") is session


async def test_owner_can_attach_and_replace_its_own_upstream():
    registry = MediaRegistry()
    session = registry.create("b-1", "VAR-1")

    first = object()
    registry.attach_upstream(session.session_id, first, agent_id="b-1")  # type: ignore[arg-type]
    second = object()
    registry.attach_upstream(session.session_id, second, agent_id="b-1")  # type: ignore[arg-type]

    assert session.upstream is second


# --- 命令服务 -----------------------------------------------------------


class FakeHub:
    """只实现 `request`：命令服务的全部依赖就这一个。"""

    def __init__(self, outcome: Exception | None = None, result: dict | None = None) -> None:
        self.outcome = outcome
        self.result = result if result is not None else {"ok": True}
        self.payloads: list[dict] = []
        self.request_ids: list[str | None] = []

    async def request(
        self, agent_id, command, payload=None, *, timeout=None, request_id=None
    ):
        self.payloads.append(dict(payload or {}))
        self.request_ids.append(request_id)
        if self.outcome is not None:
            raise self.outcome
        return self.result


@pytest.fixture()
async def service_factory(tmp_path):
    """建库并保证关闭：aiosqlite 的工作线程不关会把进程挂住。"""
    created: list[Database] = []

    async def make(hub: FakeHub) -> CommandService:
        db = Database(tmp_path / f"monitor-{len(created)}.db")
        await db.connect()
        created.append(db)
        return CommandService(db, hub)  # type: ignore[arg-type]

    try:
        yield make
    finally:
        for db in created:
            await db.close()


async def test_envelope_has_monotonic_sequence(service_factory):
    service = await service_factory(FakeHub())

    first = service.envelope(agent_id="b-1", session_id="ctl-1", action="camera.ptz")
    second = service.envelope(agent_id="b-1", session_id="ctl-1", action="camera.ptz")
    other = service.envelope(agent_id="b-1", session_id="ctl-2", action="camera.ptz")

    assert second["sequence"] == first["sequence"] + 1
    assert other["sequence"] == 1          # 另一个会话自己数
    assert first["deadline"] > first["issued_at"]
    assert first["trace_id"].startswith("tr-")
    # 幂等键按命令稳定（方案 §5.1）：同一条命令重发时键不变，设备层才能去重
    assert first["idempotency_key"] == f"camera.ptz-{first['command_id']}"
    assert first["idempotency_key"] != second["idempotency_key"]


async def test_sequence_is_not_reused_after_session_forget(service_factory):
    """会话结束后序号从 1 重来是安全的：agent 侧也按会话分组。"""
    service = await service_factory(FakeHub())
    service.envelope(agent_id="b-1", session_id="ctl-1", action="camera.ptz")

    service.forget_session("b-1", "ctl-1")

    assert service.envelope(agent_id="b-1", session_id="ctl-1", action="camera.ptz")["sequence"] == 1


async def test_dispatch_success_writes_command_log(service_factory):
    hub = FakeHub(result={"state": "succeeded"})
    service = await service_factory(hub)

    result = await service.dispatch("b-1", CommandName.CAMERA_PTZ, {"direct": "left"})

    assert result["state"] == CommandStatus.SUCCEEDED.value
    row = await service.db.fetchone("SELECT * FROM command_log WHERE command_id = ?", (result["command_id"],))
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["action"] == "camera.ptz"
    # 信封进了 payload：去重键、序号、deadline 都要能在库里查回来
    assert hub.payloads[0]["command_id"] == result["command_id"]
    assert hub.payloads[0]["sequence"] >= 1


async def test_timeout_is_unknown_not_failure(service_factory):
    """超时只说明"没等到结果"。页面必须看到 unknown，而不是 failed。"""
    hub = FakeHub(outcome=AgentError(ErrorCode.TIMEOUT, "no answer"))
    service = await service_factory(hub)

    with pytest.raises(ApiError) as raised:
        await service.dispatch("b-1", CommandName.CAMERA_PTZ, {"direct": "left"})

    assert raised.value.status_code == 504
    assert raised.value.data["state"] == CommandStatus.UNKNOWN.value
    row = await service.db.fetchone("SELECT status FROM command_log")
    assert row["status"] == "unknown"


async def test_control_held_maps_to_rejected(service_factory):
    hub = FakeHub(outcome=AgentError(ErrorCode.CONTROL_HELD, "本地人工操作中"))
    service = await service_factory(hub)

    with pytest.raises(ApiError) as raised:
        await service.dispatch("b-1", CommandName.CAMERA_PTZ, {"direct": "left"})

    assert raised.value.status_code == 409
    assert raised.value.code is ErrorCode.CONTROL_HELD
    row = await service.db.fetchone("SELECT status FROM command_log")
    assert row["status"] == "rejected"


async def test_envelope_cannot_be_overridden_by_payload(service_factory):
    """调用方传进来的 sequence/command_id 不能改信封——否则 agent 侧去重会失效。"""
    hub = FakeHub()
    service = await service_factory(hub)

    await service.dispatch(
        "b-1",
        CommandName.CAMERA_PTZ,
        {"direct": "left", "sequence": 99, "command_id": "forged"},
        command_id="real-id",
    )

    assert hub.payloads[0]["sequence"] == 1
    assert hub.payloads[0]["command_id"] == "real-id"


async def test_frame_id_equals_command_id(service_factory):
    """线上帧 id 必须与信封 `command_id` 一致（方案 §5.1）。

    不一致时，一条命令在日志里有两个标识：云端 `command_log.command_id` 是一个，
    机器人去重表/事件里是另一个。出了"点了没反应"就只能在两张表之间靠时间戳猜。
    """
    hub = FakeHub()
    service = await service_factory(hub)

    result = await service.dispatch("b-1", CommandName.CAMERA_PTZ, {"direct": "left"})

    assert hub.request_ids == [result["command_id"]]
    assert hub.payloads[0]["command_id"] == hub.request_ids[0]


# --- 云台的"单条在途"合并（方案 §5.5）---------------------------------


class SlowHub(FakeHub):
    """记录并发峰值：同一目标上不能出现两条在途命令。"""

    def __init__(self, delay: float = 0.05) -> None:
        super().__init__()
        self.delay = delay
        self.directions: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def request(
        self, agent_id, command, payload=None, *, timeout=None, request_id=None
    ):
        self.directions.append(str((payload or {}).get("direct")))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return self.result


async def test_ptz_coalesces_to_one_in_flight_command(service_factory):
    """按住方向键时并发来 5 条：只保留一条在途，其余被更新的一条覆盖。

    不合并的后果在弱网下很具体：命令堆在连接上，恢复瞬间一股脑下发，
    球机按乱序的方向转几下才停——而页面上用户早就松手了。
    """
    hub = SlowHub()
    service = await service_factory(hub)

    async def one(direct: str) -> str:
        try:
            result = await service.dispatch(
                "b-1", CommandName.CAMERA_PTZ, {"direct": direct}, session_id="s1"
            )
            return result["state"]
        except ApiError as exc:
            return exc.code.value

    states = await asyncio.gather(*(one(d) for d in ["up", "down", "left", "right", "zoom_in"]))

    assert hub.peak == 1
    # 最后一条方向发得出去，中间的都被"更新的一条"覆盖掉了
    assert hub.directions[-1] == "zoom_in"
    assert states.count(ErrorCode.STALE.value) == len(states) - len(hub.directions)
    assert service.superseded == len(states) - len(hub.directions)


async def test_superseded_command_is_logged_as_rejected(service_factory):
    """被覆盖的命令要留一行 rejected/stale，否则排障时看不到"这条根本没发"。"""
    hub = SlowHub()
    service = await service_factory(hub)

    async def one(direct: str) -> None:
        try:
            await service.dispatch(
                "b-1", CommandName.CAMERA_PTZ, {"direct": direct}, session_id="s2"
            )
        except ApiError:
            pass          # 被覆盖的那几条，预期之内

    await asyncio.gather(one("up"), one("down"), one("left"))

    rows = await service.db.fetchall(
        "SELECT status, error_code FROM command_log WHERE status = ?", ("rejected",)
    )
    assert rows and all(row["error_code"] == ErrorCode.STALE.value for row in rows)


async def test_preset_commands_are_not_coalesced(service_factory):
    """预置点是独立动作：合并等于静默丢掉用户点的那个点。"""
    hub = SlowHub()
    service = await service_factory(hub)

    await asyncio.gather(
        *(
            service.dispatch(
                "b-1", CommandName.CAMERA_PRESET_GOTO, {"preset": preset}, session_id="s3"
            )
            for preset in (1, 2, 3)
        )
    )

    assert len(hub.directions) == 3          # 三条都真的下发了
    assert hub.peak == 3                     # 互不串行


# --- 快照二段式 ---------------------------------------------------------


async def test_snapshot_returns_image_from_second_channel(service_factory):
    """命令回包只有元信息，图片从 `/internal/snapshot` 送回来。"""
    hub = FakeHub(result={"size": 9, "request_id": "x"})
    service = await service_factory(hub)

    task = asyncio.create_task(service.request_snapshot("b-1", "VAR-1"))
    for _ in range(100):
        if hub.payloads:
            break
        await asyncio.sleep(0.005)

    request_id = hub.payloads[0]["command_id"]
    assert service.deliver_snapshot(request_id, b"\xff\xd8jpeg", {"size": 9}) is True

    image, meta = await asyncio.wait_for(task, 2.0)
    assert image == b"\xff\xd8jpeg"
    assert meta["size"] == 9


async def test_snapshot_without_listener_is_rejected(service_factory):
    service = await service_factory(FakeHub())

    assert service.deliver_snapshot("nobody-waiting", b"\xff\xd8", {}) is False


async def test_snapshot_reports_command_failure_fast(service_factory):
    """命令被拒要立刻报，不能等满 12s 的超时。"""
    hub = FakeHub(outcome=AgentError(ErrorCode.CONTROL_HELD, "本地占用"))
    service = await service_factory(hub)

    started = time.monotonic()
    with pytest.raises(ApiError):
        await service.request_snapshot("b-1", "VAR-1")

    assert time.monotonic() - started < 5.0


async def test_snapshot_message_is_a_valid_frame():
    """快照命令本身走普通命令帧；这条防止有人把图塞进 payload。"""
    message = Message.command_frame(
        CommandName.CAMERA_SNAPSHOT, {"command_id": "abc", "camera_id": "VAR-1"}
    )
    assert message.command == "camera.snapshot"
    assert "image" not in message.payload


# --- 云台方向键（回归：keepalive 曾把方向改写成 stop）------------------


def test_keepalive_keeps_the_direction():
    """按住方向键时每 1.5s 一条 keepalive；方向必须保留。

    曾经写成"keepalive 一律按 stop 传"，表现是按住 1.5 秒后球机自己停下，
    而页面仍在显示"左转中"——视频里能看出来，日志里看不出来。
    """
    from robot_monitor_server.routes.camera import ptz_payload

    payload, error = ptz_payload("keepalive", "left", 4)

    assert error is None
    assert payload == {"action": "keepalive", "direct": "left", "speed": 4}


def test_stop_always_stops():
    from robot_monitor_server.routes.camera import ptz_payload

    payload, _ = ptz_payload("stop", "left", 4)
    assert payload["direct"] == "stop"


def test_keepalive_without_direction_is_rejected():
    from robot_monitor_server.routes.camera import ptz_payload

    payload, error = ptz_payload("keepalive", "stop", 4)
    assert payload is None
    assert "keepalive" in error


def test_unknown_direction_is_rejected():
    from robot_monitor_server.routes.camera import ptz_payload

    payload, error = ptz_payload("start", "sideways", 4)
    assert payload is None
    assert "sideways" in error
