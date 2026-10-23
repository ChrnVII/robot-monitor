"""WebSocket 连接注册表 + 请求/响应配对。

这是整个系统最容易写出间歇性 bug 的地方，四条规则各自对应一类事故：

1. **单写者。** 只有 `AgentConnection._send_loop` 能调用 `websocket.send_text`。
   任何两个协程并发 send 都会交错帧、破坏流，而且触发条件随机——压测能偶现、
   线上日志看不出来。所以别的代码只能往 outbox 里放字符串。

2. **每个请求一个 Future，且必须带超时。** WebSocket 没有请求/响应语义，
   `id` 是唯一的关联手段。没有超时的话 agent 不回包就永久占一个 Future，
   最终表现为内存缓慢上涨 + 前端一直转圈。

3. **指令队列有界且阻塞等待，遥测满了直接丢。** 有界防 OOM；阻塞而不是丢，
   因为指令丢了就没人重发。队列满说明 agent 卡住了，这时应该报错而不是
   无限堆积。

4. **连接断开时必须把挂起的 Future 全部失败掉。** 否则调用方会一直等到
   自己的超时，而那时错误码会变成 `timeout`——但真实原因是 `agent_offline`，
   两者前端要展示的东西完全不同。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from .auth import AuthService
from .config import Settings
from .events import EventBus
from .protocol import CommandName, ErrorCode, Message

logger = logging.getLogger("filebridge.hub")

# 单连接积压的指令上限。超过说明 agent 没在处理，继续堆积没有意义。
OUTBOX_SIZE = 64


class AgentError(Exception):
    """指令没能完成。code 直接映射到前端的展示逻辑。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True)
class AgentMeta:
    id: str
    name: str
    hostname: str | None = None
    os: str | None = None
    agent_version: str | None = None
    roots: list[str] = field(default_factory=list)


class AgentConnection:
    """一台 B 的控制连接。"""

    def __init__(
        self,
        websocket: WebSocket,
        meta: AgentMeta,
        *,
        command_timeout: float = 10.0,
    ) -> None:
        self.websocket = websocket
        self.meta = meta
        self.agent_id = meta.id
        self.command_timeout = command_timeout
        self.outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=OUTBOX_SIZE)
        self.pending: dict[str, asyncio.Future[Message]] = {}
        self.last_seen = time.time()
        self.connected_at = self.last_seen
        self.closed = False
        self._send_task: asyncio.Task[None] | None = None

    # --- 生命周期 -------------------------------------------------------

    def start(self) -> None:
        self._send_task = asyncio.create_task(
            self._send_loop(), name=f"agent-send-{self.agent_id}"
        )

    async def _send_loop(self) -> None:
        """唯一的写者。除了这里，谁都不许碰 socket 的 send。"""
        try:
            while True:
                frame = await self.outbox.get()
                await self.websocket.send_text(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 这里就是要吞掉所有发送异常
            # socket 坏掉时异常类型五花八门（ConnectionClosed、RuntimeError、
            # OSError……）；对 send loop 来说它们只有一个含义：连接没了。
            logger.info("agent %s send loop stopped: %s", self.agent_id, exc)
            self.abort("connection lost while sending")

    def abort(self, reason: str) -> None:
        """标记连接不可用，并让所有等待中的请求立刻失败。"""
        if self.closed:
            return
        self.closed = True
        self._fail_pending(reason)

    async def shutdown(self, reason: str, *, code: int = 4409) -> None:
        """主动关掉连接：先停写者，再关 socket，避免 send/send 并发。"""
        self.abort(reason)
        task = self._send_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code, reason=reason[:120])

    # --- 发送 -----------------------------------------------------------

    async def enqueue(self, frame: str, *, timeout: float = 5.0) -> None:
        if self.closed:
            raise AgentError(ErrorCode.AGENT_OFFLINE, "agent connection is closed")
        try:
            await asyncio.wait_for(self.outbox.put(frame), timeout)
        except TimeoutError as exc:
            raise AgentError(
                ErrorCode.RATE_LIMITED, "agent command queue is full; it is not keeping up"
            ) from exc

    # --- 请求/响应 ------------------------------------------------------

    async def request(
        self,
        command: CommandName,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """下发一条指令并等它的 result。失败抛 AgentError。"""
        if self.closed:
            raise AgentError(ErrorCode.AGENT_OFFLINE, "agent is not connected")

        frame = Message.command_frame(command, payload or {})
        request_id = frame.id
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future

        limit = timeout if timeout is not None else self.command_timeout
        try:
            await self.enqueue(frame.to_json())
            message = await asyncio.wait_for(future, limit)
        except TimeoutError as exc:
            raise AgentError(
                ErrorCode.TIMEOUT, f"agent did not answer {command.value} within {limit:g}s"
            ) from exc
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

        if message.ok is False:
            detail = message.error
            raise AgentError(
                detail.code if detail else ErrorCode.INTERNAL,
                detail.message if detail else "agent returned an error without a code",
            )
        return message.payload

    def resolve(self, message: Message) -> bool:
        """把一个 result 帧交给它对应的请求。返回是否命中。"""
        future = self.pending.get(message.id)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    def _fail_pending(self, reason: str) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(AgentError(ErrorCode.AGENT_OFFLINE, reason))
        self.pending.clear()


class Hub:
    """agent_id → 连接。单进程内存，因此 uvicorn 必须单 worker。"""

    def __init__(
        self,
        auth: AuthService,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self.auth = auth
        self.bus = bus
        self.settings = settings
        self._connections: dict[str, AgentConnection] = {}
        self._sweeper: asyncio.Task[None] | None = None

    # --- 注册 -----------------------------------------------------------

    async def register(self, conn: AgentConnection) -> None:
        previous = self._connections.get(conn.agent_id)
        self._connections[conn.agent_id] = conn

        if previous is not None and previous is not conn:
            # 同一台机器重连（旧连接可能还挂着半死状态）：新连接赢，
            # 旧连接上的挂起请求立刻失败，不要让调用方等到超时。
            logger.info("agent %s reconnected, dropping stale connection", conn.agent_id)
            await previous.shutdown("replaced by a newer connection", code=4410)

        await self.auth.mark_agent_online(conn.agent_id, online=True)
        self.bus.publish(
            "agent_online",
            {
                "agent_id": conn.agent_id,
                "name": conn.meta.name,
                "hostname": conn.meta.hostname,
            },
        )
        logger.info("agent %s (%s) online", conn.agent_id, conn.meta.name)

    async def unregister(self, conn: AgentConnection) -> None:
        if self._connections.get(conn.agent_id) is not conn:
            return
        self._connections.pop(conn.agent_id, None)
        conn.abort("disconnected")
        await self.auth.mark_agent_online(conn.agent_id, online=False)
        self.bus.publish(
            "agent_offline", {"agent_id": conn.agent_id, "name": conn.meta.name}
        )
        logger.info("agent %s offline", conn.agent_id)

    # --- 查询 -----------------------------------------------------------

    def get(self, agent_id: str) -> AgentConnection | None:
        conn = self._connections.get(agent_id)
        if conn is None or conn.closed:
            return None
        return conn

    def is_online(self, agent_id: str) -> bool:
        return self.get(agent_id) is not None

    def online_ids(self) -> list[str]:
        return [aid for aid in self._connections if self.is_online(aid)]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            aid: {
                "agent_id": aid,
                "name": conn.meta.name,
                "hostname": conn.meta.hostname,
                "os": conn.meta.os,
                "agent_version": conn.meta.agent_version,
                "connected_at": conn.connected_at,
                "last_seen": conn.last_seen,
                "pending_requests": len(conn.pending),
            }
            for aid, conn in self._connections.items()
            if not conn.closed
        }

    # --- 请求 -----------------------------------------------------------

    async def request(
        self,
        agent_id: str,
        command: CommandName,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        conn = self.get(agent_id)
        if conn is None:
            raise AgentError(ErrorCode.AGENT_OFFLINE, "agent is not connected")
        return await conn.request(command, payload, timeout=timeout)

    # --- 心跳清扫 -------------------------------------------------------

    def start_sweeper(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="agent-heartbeat-sweeper")

    async def stop_sweeper(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

    async def _sweep_loop(self) -> None:
        """socket 还在、但心跳停了，也要判离线。

        半开连接（NAT 超时、对端断电）不会触发 WebSocket 的 close 事件，
        只能靠"多久没收到任何帧"来发现。
        """
        while True:
            await asyncio.sleep(self.settings.heartbeat_sweep_seconds)
            deadline = time.time() - self.settings.heartbeat_timeout_seconds
            for conn in list(self._connections.values()):
                if conn.closed:
                    continue
                if conn.last_seen < deadline:
                    logger.warning("agent %s heartbeat timeout, closing", conn.agent_id)
                    await conn.shutdown("heartbeat timeout", code=4408)
                    await self.unregister(conn)

    async def close_all(self) -> None:
        for conn in list(self._connections.values()):
            await conn.shutdown("server shutting down", code=1001)
            await self.unregister(conn)
