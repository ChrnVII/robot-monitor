"""hub 的配对、超时与并发测试。

这三个是这个系统最容易出现"随机失败"的地方，所以每个都用假 socket 精确验证，
不依赖网络。
"""

from __future__ import annotations

import asyncio

import pytest

from filebridge_server.hub import AgentConnection, AgentError, AgentMeta
from filebridge_server.protocol import CommandName, ErrorCode, Message


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False
        self.close_code: int | None = None

    async def send_text(self, frame: str) -> None:
        await self.sent.put(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code


@pytest.fixture()
async def conn():
    ws = FakeWebSocket()
    connection = AgentConnection(ws, AgentMeta(id="a1", name="test"), command_timeout=0.5)
    connection.start()
    try:
        yield connection, ws
    finally:
        await connection.shutdown("test done")


async def test_request_pairs_response_by_id(conn):
    connection, ws = conn

    pending = asyncio.create_task(connection.request(CommandName.PING, {"echo": 1}))
    frame = Message.parse(await asyncio.wait_for(ws.sent.get(), 1))

    assert frame.command == "ping"
    assert frame.payload == {"echo": 1}

    connection.resolve(Message.result_frame(frame.id, {"pong": True}))
    assert await asyncio.wait_for(pending, 1) == {"pong": True}


async def test_timeout_raises_and_leaves_no_pending(conn):
    connection, _ws = conn

    with pytest.raises(AgentError) as excinfo:
        await connection.request(CommandName.LIST_DIR, {"rel": ""}, timeout=0.05)

    assert excinfo.value.code is ErrorCode.TIMEOUT
    # 超时后必须把 Future 摘掉，否则挂起的请求会一直累积
    assert connection.pending == {}


async def test_concurrent_requests_do_not_cross(conn):
    """5 个请求同时在飞，乱序回包，每个都必须拿到自己的结果。"""
    connection, ws = conn

    tasks = [
        asyncio.create_task(connection.request(CommandName.PING, {"echo": index}))
        for index in range(5)
    ]

    frames = [Message.parse(await asyncio.wait_for(ws.sent.get(), 1)) for _ in range(5)]
    assert len({frame.id for frame in frames}) == 5

    # 故意倒序回，模拟 B 侧并发处理完成顺序不确定
    for frame in reversed(frames):
        connection.resolve(
            Message.result_frame(frame.id, {"echo": frame.payload["echo"], "id": frame.id})
        )

    results = await asyncio.gather(*tasks)
    for index, result in enumerate(results):
        assert result["echo"] == index
        assert result["id"] == frames[index].id


async def test_error_frame_becomes_agent_error(conn):
    connection, ws = conn

    pending = asyncio.create_task(connection.request(CommandName.STAT_FILE, {"rel": "x"}))
    frame = Message.parse(await asyncio.wait_for(ws.sent.get(), 1))
    connection.resolve(Message.error_frame(frame.id, ErrorCode.FORBIDDEN, "path escapes root"))

    with pytest.raises(AgentError) as excinfo:
        await pending

    assert excinfo.value.code is ErrorCode.FORBIDDEN
    assert "escapes" in excinfo.value.message


async def test_abort_fails_all_pending_with_offline(conn):
    connection, _ws = conn

    tasks = [
        asyncio.create_task(connection.request(CommandName.PING, {})) for _ in range(3)
    ]
    await asyncio.sleep(0.01)

    connection.abort("socket died")

    for task in tasks:
        with pytest.raises(AgentError) as excinfo:
            await task
        # 关键：错误码必须是 agent_offline 而不是 timeout，
        # 前端要靠它决定"能不能重试"
        assert excinfo.value.code is ErrorCode.AGENT_OFFLINE
    assert connection.pending == {}


async def test_request_after_abort_is_rejected(conn):
    connection, _ws = conn
    connection.abort("done")

    with pytest.raises(AgentError) as excinfo:
        await connection.request(CommandName.PING, {})
    assert excinfo.value.code is ErrorCode.AGENT_OFFLINE


async def test_bounded_outbox_reports_backpressure():
    ws = FakeWebSocket()
    # 不启动 send loop，队列会一直积压
    connection = AgentConnection(ws, AgentMeta(id="a2", name="slow"), command_timeout=0.2)

    for _ in range(64):
        await connection.enqueue("{}", timeout=0.1)

    with pytest.raises(AgentError) as excinfo:
        await connection.enqueue("{}", timeout=0.01)

    assert excinfo.value.code is ErrorCode.RATE_LIMITED
