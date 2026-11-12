"""测试专用的“机器人”：只实现 server 需要的那部分协议。

为什么不再 import 旧 agent：`monitor/agent` 已经由 `robot-backend/app/cloud_agent`
取代，server 的端到端测试不应该依赖一个已删除的项目。这里用一个最小实现顶替
机器人侧，让 server 测试自包含：

- 会连 `/ws/agent`、发 hello、回 ping / list_dir / stat_file / hash_file /
  start_upload / cancel_task；
- 会按 /internal/uploads/* 真实分片上传并提交 sha256；
- 不实现注册、凭据、暂停等“云端接入”功能——那是 cloud-agent 自己的测试范围。

它不是生产代码，也不会被打包；只用于验证 server 的 WS、分片、判重、SSE 链路。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

from robot_monitor_server.protocol import (
    MAX_FRAME_BYTES,
    CommandName,
    ErrorCode,
    Message,
    MessageType,
)

READ_CHUNK = 1024 * 1024


class MiniAgentError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MiniAgent:
    def __init__(
        self,
        root: Path,
        base_url: str,
        token: str,
        *,
        name: str = "e2e-agent",
        heartbeat_interval: float = 0.5,
    ) -> None:
        self.root = Path(root).resolve()
        self.base = base_url.rstrip("/")
        self.token = token
        self.name = name
        self.heartbeat_interval = heartbeat_interval
        self.ws_url = self.base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        self.ws_url += "/ws/agent"

        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        self._handlers: set[asyncio.Task[None]] = set()
        self._uploads: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._http_client: httpx.AsyncClient | None = None

    # --- 生命周期 -------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        async with websockets.connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=MAX_FRAME_BYTES * 4,
            open_timeout=5,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            sender = asyncio.create_task(self._send_loop(), name="mini-agent-send")
            heartbeat = asyncio.create_task(self._heartbeat(), name="mini-agent-heartbeat")
            try:
                await self._enqueue(self._hello().to_json())
                async for raw in ws:
                    await self._on_frame(raw)
            finally:
                for task in (sender, heartbeat):
                    task.cancel()
                await asyncio.gather(sender, heartbeat, return_exceptions=True)
                self._ws = None
                for task in list(self._handlers):
                    task.cancel()
                if self._handlers:
                    await asyncio.gather(*self._handlers, return_exceptions=True)
                self._handlers.clear()
                await self._shutdown_uploads()

    def stop(self) -> None:
        """供测试主线程调用：把关闭动作投递回 agent 自己的事件循环。"""
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._close_soon)

    def _close_soon(self) -> None:
        if self._ws is not None:
            asyncio.create_task(self._ws.close())

    def _hello(self) -> Message:
        return Message.hello_frame(
            self.name,
            {
                "hostname": self.name,
                "os": "linux",
                "agent_version": "e2e-mini",
                "roots": [str(self.root)],
            },
        )

    # --- 发送 -----------------------------------------------------------

    async def _send_loop(self) -> None:
        while True:
            frame = await self._outbox.get()
            await self._ws.send(frame)

    async def _enqueue(self, frame: str) -> None:
        await asyncio.wait_for(self._outbox.put(frame), 5.0)

    async def _enqueue_nowait(self, frame: str) -> None:
        try:
            self._outbox.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    async def _reply(self, message: Message) -> None:
        await self._enqueue(message.to_json())

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.heartbeat_interval)
            frame = Message(
                type=MessageType.EVENT,
                payload={
                    "kind": "heartbeat",
                    "ts": time.time(),
                    "active_uploads": sum(1 for task in self._uploads.values() if not task.done()),
                },
            ).to_json()
            self._enqueue_nowait(frame)

    # --- 接收 -----------------------------------------------------------

    async def _on_frame(self, raw: str | bytes) -> None:
        message = Message.parse(raw)
        if message.type is MessageType.COMMAND:
            self._spawn(self._handle_command(message))
        elif message.type is MessageType.ACK:
            for item in message.payload.get("resume_uploads") or []:
                if isinstance(item, dict):
                    self._start_upload(item)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle_command(self, message: Message) -> None:
        command = message.command_name
        try:
            if command is None:
                raise MiniAgentError(ErrorCode.UNKNOWN_COMMAND, f"不支持的命令：{message.command}")
            result = await self._dispatch(command, message.payload)
        except MiniAgentError as exc:
            await self._reply(Message.error_frame(message.id, exc.code, exc.message))
        except Exception as exc:  # noqa: BLE001 - 测试替身：任何异常都回给 server
            await self._reply(Message.error_frame(message.id, ErrorCode.INTERNAL, str(exc)))
        else:
            await self._reply(Message.result_frame(message.id, result))

    async def _dispatch(self, command: CommandName, payload: dict[str, Any]) -> dict[str, Any]:
        if command is CommandName.PING:
            return {"pong": time.time(), "echo": payload.get("echo")}
        if command is CommandName.LIST_DIR:
            return await asyncio.to_thread(self._list_dir, payload)
        if command is CommandName.STAT_FILE:
            return await asyncio.to_thread(self._stat_file, payload)
        if command is CommandName.HASH_FILE:
            return await asyncio.to_thread(self._hash_file, payload)
        if command is CommandName.START_UPLOAD:
            return self._accept_upload(payload)
        if command is CommandName.CANCEL_TASK:
            upload_id = str(payload.get("upload_id") or "")
            task = self._uploads.get(upload_id)
            self._cancelled.add(upload_id)
            if task is not None and not task.done():
                task.cancel()
            return {"canceled": upload_id}
        raise MiniAgentError(ErrorCode.UNKNOWN_COMMAND, f"不支持的命令：{command.value}")

    # --- 文件能力 -------------------------------------------------------

    def _resolve(self, rel: Any) -> Path:
        if not isinstance(rel, str):
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "path must be a string")
        if "\x00" in rel:
            raise MiniAgentError(ErrorCode.FORBIDDEN, "path contains a null byte")
        if rel.startswith("/"):
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "absolute paths are not accepted")
        segments = [segment for segment in rel.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "path traversal is not accepted")

        candidate = self.root.joinpath(*segments)
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise MiniAgentError(ErrorCode.FORBIDDEN, str(exc)) from exc
        if resolved != self.root and self.root not in resolved.parents:
            raise MiniAgentError(ErrorCode.FORBIDDEN, f"path escapes root: {rel!r}")
        return resolved

    def _stat_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        rel = str(payload.get("rel") or "")
        if not rel:
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "需要指定文件路径")
        target = self._resolve(rel)
        try:
            info = target.stat()
        except FileNotFoundError as exc:
            raise MiniAgentError(ErrorCode.FILE_NOT_FOUND, f"文件不存在：{rel}") from exc
        except OSError as exc:
            raise MiniAgentError(ErrorCode.FORBIDDEN, f"无法访问 {rel}：{exc}") from exc

        if target.is_dir():
            return {"rel": rel, "name": target.name, "is_dir": True, "size": 0, "mtime": info.st_mtime}

        try:
            with open(target, "rb") as handle:
                handle.read(1)
        except OSError as exc:
            raise MiniAgentError(ErrorCode.FORBIDDEN, f"无法读取：{rel}：{exc}") from exc

        return {
            "rel": rel,
            "name": target.name,
            "is_dir": False,
            "size": info.st_size,
            "mtime": info.st_mtime,
        }

    def _list_dir(self, payload: dict[str, Any]) -> dict[str, Any]:
        rel = str(payload.get("rel") or "")
        limit = _clamp(payload.get("limit"), 200, 1, 1000)
        cursor = _cursor(payload.get("cursor"))
        target = self._resolve(rel)
        if not target.is_dir():
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, f"{rel!r} 不是目录")

        entries: list[dict[str, Any]] = []
        with os.scandir(target) as iterator:
            for entry in iterator:
                try:
                    if entry.is_symlink():
                        info = entry.stat(follow_symlinks=False)
                        kind = "symlink"
                    elif entry.is_dir(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        kind = "dir"
                    elif entry.is_file(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        kind = "file"
                    else:
                        continue
                except OSError:
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "rel": f"{rel}/{entry.name}" if rel else entry.name,
                        "type": kind,
                        "size": info.st_size if kind != "dir" else 0,
                        "mtime": info.st_mtime,
                    }
                )

        entries.sort(key=lambda item: (item["type"] != "dir", str(item["name"]).casefold()))
        page = entries[cursor : cursor + limit]
        next_cursor = cursor + limit if cursor + limit < len(entries) else None
        return {
            "path": rel,
            "entries": page,
            "total": len(entries),
            "next_cursor": str(next_cursor) if next_cursor is not None else None,
        }

    def _hash_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        rel = str(payload.get("rel") or "")
        if not rel:
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "需要指定文件路径")
        target = self._resolve(rel)
        digest = hashlib.sha256()
        size = 0
        try:
            with open(target, "rb") as handle:
                while block := handle.read(READ_CHUNK):
                    digest.update(block)
                    size += len(block)
        except FileNotFoundError as exc:
            raise MiniAgentError(ErrorCode.FILE_NOT_FOUND, f"文件不存在：{rel}") from exc
        except OSError as exc:
            raise MiniAgentError(ErrorCode.FORBIDDEN, f"无法读取：{rel}：{exc}") from exc
        return {"rel": rel, "sha256": digest.hexdigest(), "size": size}

    # --- 上传 -----------------------------------------------------------

    def _accept_upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        upload_id = payload.get("upload_id")
        rel = payload.get("rel")
        if not isinstance(upload_id, str) or not upload_id:
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "upload_id 不合法")
        if not isinstance(rel, str) or not rel:
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "rel 不合法")

        try:
            total_size = int(payload.get("total_size"))
            chunk_size = int(payload.get("chunk_size"))
            total_chunks = int(payload.get("total_chunks"))
        except (TypeError, ValueError) as exc:
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "分块参数不是整数") from exc
        expected = math.ceil(total_size / chunk_size) if total_size else 0
        if chunk_size <= 0 or total_chunks != expected:
            raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "分块参数不一致")

        stat = self._stat_file({"rel": rel})
        if stat["is_dir"]:
            raise MiniAgentError(ErrorCode.NOT_A_FILE, f"{rel} 是目录")
        if int(stat["size"]) != total_size:
            raise MiniAgentError(ErrorCode.SIZE_CHANGED, f"{rel} 大小已变化")

        self._start_upload(payload)
        return {"accepted": True, "upload_id": upload_id}

    def _start_upload(self, payload: dict[str, Any]) -> None:
        upload_id = str(payload.get("upload_id") or "")
        if not upload_id:
            return
        running = self._uploads.get(upload_id)
        if running is not None and not running.done():
            return
        self._cancelled.discard(upload_id)
        task = asyncio.create_task(self._upload(payload), name=f"mini-upload-{upload_id[:8]}")
        self._uploads[upload_id] = task
        task.add_done_callback(lambda _: self._uploads.pop(upload_id, None))

    async def _upload(self, payload: dict[str, Any]) -> None:
        upload_id = str(payload["upload_id"])
        rel = str(payload["rel"])
        total_size = int(payload["total_size"])
        chunk_size = int(payload["chunk_size"])
        total_chunks = int(payload["total_chunks"])

        try:
            target = self._resolve(rel)
            status = await self._http().get(f"/internal/uploads/{upload_id}/status")
            if status.status_code == 404:
                return
            status.raise_for_status()
            received = {int(index) for index in status.json().get("received") or []}

            before = target.stat()
            if before.st_size != total_size:
                raise MiniAgentError(ErrorCode.SIZE_CHANGED, f"{rel} 大小已变化")

            # 测试文件最大 8 MiB，整份读进内存比在协程里跨块调度简单；
            # 省下的是事件循环阻塞，代价可以忽略。（真实 agent 是流式读的。）
            data = await asyncio.to_thread(target.read_bytes)
            if len(data) != total_size:
                raise MiniAgentError(ErrorCode.SIZE_CHANGED, f"{rel} 读取长度不对")
            digest = hashlib.sha256(data)

            for index in range(total_chunks):
                if upload_id in self._cancelled:
                    return
                offset = index * chunk_size
                expected = min(chunk_size, total_size - offset)
                chunk = data[offset : offset + expected]
                if len(chunk) != expected:
                    raise MiniAgentError(ErrorCode.SIZE_CHANGED, f"第 {index} 块长度不对")
                if index in received:
                    continue
                await self._put_chunk(upload_id, index, chunk)

            after = target.stat()
            if after.st_size != before.st_size or after.st_mtime != before.st_mtime:
                raise MiniAgentError(ErrorCode.SIZE_CHANGED, f"{rel} 在上传过程中被修改")

            response = await self._http().post(
                f"/internal/uploads/{upload_id}/complete",
                json={"sha256": digest.hexdigest()},
                timeout=120.0,
            )
            if response.status_code not in (200, 404):
                response.raise_for_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败上报给 server，别静默
            await self._report_failure(upload_id, str(exc))

    async def _put_chunk(self, upload_id: str, index: int, chunk: bytes) -> None:
        last_error: object = None
        for attempt in range(3):
            try:
                response = await self._http().put(
                    f"/internal/uploads/{upload_id}/chunks/{index}", content=chunk
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    return
                if response.status_code in {400, 401, 403, 404, 409, 411, 413, 507}:
                    response.raise_for_status()
                last_error = f"HTTP {response.status_code}"
            await asyncio.sleep(0.05 * (2**attempt))
        raise MiniAgentError(ErrorCode.INTERNAL, f"第 {index} 块重试后仍失败：{last_error}")

    async def _report_failure(self, upload_id: str, message: str) -> None:
        frame = Message(
            type=MessageType.EVENT,
            payload={
                "kind": "upload_failed",
                "upload_id": upload_id,
                "code": ErrorCode.INTERNAL.value,
                "message": message,
            },
        ).to_json()
        self._enqueue_nowait(frame)

    async def _shutdown_uploads(self) -> None:
        for task in list(self._uploads.values()):
            task.cancel()
        if self._uploads:
            await asyncio.gather(*self._uploads.values(), return_exceptions=True)
        self._uploads.clear()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.base,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=httpx.Timeout(60.0),
            )
        return self._http_client


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "limit 必须是整数") from exc
    return max(low, min(high, number))


def _cursor(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        offset = int(str(value))
    except (TypeError, ValueError) as exc:
        raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "cursor 不合法") from exc
    if offset < 0:
        raise MiniAgentError(ErrorCode.INVALID_ARGUMENTS, "cursor 不合法")
    return offset
