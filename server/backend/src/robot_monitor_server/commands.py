"""命令服务：信封、状态机、快照的二次通道。

三件事，对应方案 §5 的三条硬要求：

1. **每条命令都有信封。** `command_id`（去重）、`sequence`（新旧）、`deadline`
   （过期不执行）、`session_id`（绑租约）、`trace_id`（贯穿两端）。没有信封的
   云台命令在弱网下必然出问题：重发转两下、乱序停在错方向、积压的旧方向在
   恢复瞬间抽搐。
2. **状态机落库，`unknown` 是一种明确结论。** 超时只说明"没等到结果"，
   不代表机器人没执行——页面必须显示"结果未知"，并给出"查一下当前预置点"
   的纠正入口，而不是假装成功或假装失败。
3. **快照走二段式。** 命令只带元信息，图像本体由 agent 通过
   `POST /internal/snapshot/{request_id}` 送回来；`request_id` 就是 command_id，
   两边不用再对一次账。

关于"202 只代表 accepted"：本服务是**同步等到终态**的（上限由 deadline 决定，
云台 3s 内必有答案）。异步化留给 P3 的事件通道——那时页面拿到的是
`succeeded` 事件，而不是 HTTP 200。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any

from .db import Database
from .errors import ApiError
from .hub import AgentError, Hub
from .protocol import CommandName, ErrorCode, new_id

logger = logging.getLogger("robot-monitor.commands")

#: 云台命令的存活时间：短到不会让积压的旧方向在恢复后被执行，
#: 长到够一次本机 HTTP 往返（弱网下 RTT 可能到 1s）。
DEFAULT_DEADLINE_SECONDS = 3.0
#: 抓图要等相机出图，给得宽一点。
SNAPSHOT_DEADLINE_SECONDS = 8.0
SNAPSHOT_TIMEOUT = 12.0


class CommandStatus(str, Enum):
    ACCEPTED = "accepted"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


#: agent 侧错误码 → 云端命令终态。没有列出的（如 internal）算 failed。
_STATUS_BY_CODE = {
    ErrorCode.AGENT_OFFLINE: CommandStatus.FAILED.value,
    ErrorCode.EXPIRED: CommandStatus.EXPIRED.value,
    ErrorCode.STALE: CommandStatus.REJECTED.value,
    ErrorCode.CONTROL_HELD: CommandStatus.REJECTED.value,
    ErrorCode.FORBIDDEN: CommandStatus.REJECTED.value,
    ErrorCode.INVALID_ARGUMENTS: CommandStatus.REJECTED.value,
    ErrorCode.UNKNOWN_COMMAND: CommandStatus.REJECTED.value,
    ErrorCode.CAMERA_UNAVAILABLE: CommandStatus.FAILED.value,
    ErrorCode.STREAM_DISABLED: CommandStatus.FAILED.value,
}


class CommandService:
    def __init__(self, db: Database, hub: Hub) -> None:
        self.db = db
        self.hub = hub
        #: (agent, session, target) → 最后用过的序号。单调递增，不重用。
        self._sequences: dict[tuple[str, str, str], int] = {}
        #: request_id → 等图的 Future。
        self._images: dict[str, asyncio.Future[tuple[bytes, dict[str, Any]]]] = {}

    # --- 信封 -----------------------------------------------------------

    def next_sequence(self, agent_id: str, session_id: str, target: str = "camera") -> int:
        key = (agent_id, session_id, target)
        self._sequences[key] = self._sequences.get(key, 0) + 1
        return self._sequences[key]

    def forget_session(self, agent_id: str, session_id: str) -> None:
        for key in [k for k in self._sequences if k[0] == agent_id and k[1] == session_id]:
            self._sequences.pop(key, None)

    def envelope(
        self,
        *,
        agent_id: str,
        session_id: str,
        action: str,
        target: str = "camera",
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        trace_id: str = "",
        command_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        identifier = command_id or new_id()
        return {
            "command_id": identifier,
            "session_id": session_id,
            "sequence": self.next_sequence(agent_id, session_id, target),
            "action": action,
            "target": target,
            "issued_at": now,
            "deadline": now + deadline_seconds,
            "trace_id": trace_id or f"tr-{identifier[:8]}",
        }

    # --- 下发 -----------------------------------------------------------

    async def dispatch(
        self,
        agent_id: str,
        command: CommandName,
        payload: dict[str, Any],
        *,
        session_id: str = "",
        target: str = "camera",
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        trace_id: str = "",
        command_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """下发并使终态落库。返回给路由的 body，不含任何"假装成功"。"""
        envelope = self.envelope(
            agent_id=agent_id,
            session_id=session_id,
            action=command.value,
            target=target,
            deadline_seconds=deadline_seconds,
            trace_id=trace_id,
            command_id=command_id,
        )
        # payload 只能补字段，不能覆盖信封（信封是权威，否则 agent 侧
        # 的去重与序号会被调用方传进来的值悄悄改掉）。
        envelope = {**payload, **envelope}
        envelope["agent_id"] = agent_id

        await self._log(envelope, command, CommandStatus.ACCEPTED)
        limit = timeout if timeout is not None else max(
            1.0, envelope["deadline"] - time.time()
        ) + 2.0

        try:
            result = await self.hub.request(agent_id, command, envelope, timeout=limit)
        except AgentError as exc:
            status = _STATUS_BY_CODE.get(exc.code)
            if status is None:
                status = (
                    CommandStatus.UNKNOWN.value
                    if exc.code is ErrorCode.TIMEOUT
                    else CommandStatus.FAILED.value
                )
            await self._log(envelope, command, status, error_code=exc.code.value)
            raise ApiError(
                _http_status(exc.code),
                exc.code,
                exc.message,
                data={
                    "command_id": envelope["command_id"],
                    "state": status,
                    "trace_id": envelope["trace_id"],
                },
            ) from exc

        await self._log(envelope, command, CommandStatus.SUCCEEDED)
        return {
            "command_id": envelope["command_id"],
            "state": CommandStatus.SUCCEEDED.value,
            "trace_id": envelope["trace_id"],
            "result": result,
        }

    # --- 快照 -----------------------------------------------------------

    async def request_snapshot(
        self, agent_id: str, camera_id: str, *, session_id: str = "", trace_id: str = ""
    ) -> tuple[bytes, dict[str, Any]]:
        """下发 `camera.snapshot` 并等图片回来。

        两个通道同时等：命令回包（元信息/失败原因）与图片本体。哪个先到都能
        立刻决定结果——只等图片会掩盖"命令被拒"（要等满 12s），只等命令
        又会漏掉"图丢了"。
        """
        request_id = new_id()
        future: asyncio.Future[tuple[bytes, dict[str, Any]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._images[request_id] = future
        task = asyncio.create_task(
            self.dispatch(
                agent_id,
                CommandName.CAMERA_SNAPSHOT,
                {"camera_id": camera_id} if camera_id else {},
                session_id=session_id,
                deadline_seconds=SNAPSHOT_DEADLINE_SECONDS,
                trace_id=trace_id,
                command_id=request_id,
            )
        )
        try:
            done, _ = await asyncio.wait(
                {future, task}, timeout=SNAPSHOT_TIMEOUT, return_when=asyncio.FIRST_COMPLETED
            )
            if future in done:
                return future.result()
            if task in done:
                # 命令先结束：要么失败（异常已经翻成 ApiError），要么成功但图丢了。
                task.result()
                raise ApiError(
                    504,
                    ErrorCode.TIMEOUT,
                    "相机拍到了，但图片没有回传上来（命令成功、图像缺失）",
                    data={"command_id": request_id, "state": CommandStatus.UNKNOWN.value},
                )
            raise ApiError(
                504,
                ErrorCode.TIMEOUT,
                "等待相机出图超时",
                data={"command_id": request_id, "state": CommandStatus.UNKNOWN.value},
            )
        finally:
            self._images.pop(request_id, None)
            if not task.done():
                # 图片先到了：命令自身的结果不再影响本次响应，让它自己收尾。
                task.add_done_callback(_swallow)

    def deliver_snapshot(
        self, request_id: str, image: bytes, meta: dict[str, Any]
    ) -> bool:
        """agent 回传的图片落到这里。返回是否有人还在等。"""
        future = self._images.get(request_id)
        if future is None or future.done():
            return False
        future.set_result((image, meta))
        return True

    # --- 命令日志 -------------------------------------------------------

    async def _log(
        self,
        envelope: dict[str, Any],
        command: CommandName,
        status: CommandStatus | str,
        *,
        error_code: str | None = None,
    ) -> None:
        """写 command_log。审计失败不能影响命令本身的结论。"""
        try:
            await self.db.execute(
                "INSERT INTO command_log (command_id, agent_id, session_id, action, status,"
                " issued_at, deadline, updated_at, trace_id, error_code, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(command_id) DO UPDATE SET status = excluded.status,"
                " updated_at = excluded.updated_at, error_code = excluded.error_code",
                (
                    envelope["command_id"],
                    envelope.get("agent_id") or "",
                    envelope.get("session_id") or "",
                    command.value,
                    status.value if isinstance(status, CommandStatus) else str(status),
                    float(envelope.get("issued_at") or 0.0),
                    float(envelope.get("deadline") or 0.0),
                    time.time(),
                    envelope.get("trace_id") or "",
                    error_code,
                    json.dumps(
                        {k: v for k, v in envelope.items() if k not in {"payload_json"}},
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            )
        except Exception:
            logger.exception("command_log 写入失败：%s", envelope.get("command_id"))

    async def recent(self, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT command_id, action, status, issued_at, updated_at, trace_id, error_code"
            " FROM command_log WHERE agent_id = ? ORDER BY issued_at DESC LIMIT ?",
            (agent_id, max(1, min(limit, 200))),
        )
        return [dict(row) for row in rows]


def _swallow(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("后台命令收尾时失败：%s", exc)


def _http_status(code: ErrorCode) -> int:
    mapping = {
        ErrorCode.AGENT_OFFLINE: 409,
        ErrorCode.TIMEOUT: 504,
        ErrorCode.EXPIRED: 409,
        ErrorCode.STALE: 409,
        ErrorCode.CONTROL_HELD: 409,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.INVALID_ARGUMENTS: 400,
        ErrorCode.UNKNOWN_COMMAND: 400,
        ErrorCode.CAMERA_UNAVAILABLE: 503,
        ErrorCode.STREAM_DISABLED: 503,
    }
    return mapping.get(code, 502)
