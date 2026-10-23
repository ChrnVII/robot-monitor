"""分片上传与断点续传。

时序：拿 status → 顺序读文件（同时算 sha256）→ 缺失的块 3 路并发 PUT → complete。

为什么"顺序读"和"并发传"能共存：读永远按块序，读完的块进一个小批次（不超过
并发数），批次满了才一起发出去。这样哈希是对的（顺序），网络也是并发的。
如果让三个 worker 各自 seek 随机位置读，哈希顺序就乱了，sha256 只能整体重算
一遍——那是两次磁盘 IO。

代价是**续传时要重读已上传的块**，因为 complete 需要一个覆盖全文件的 sha256。
读盘比传网络便宜得多，用一次重读换"协议里不需要为每块维护哈希"，值得。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import AgentConfig
from .errors import AgentCommandError, UploadCancelled, UploadFailed
from .protocol import ErrorCode
from .sandbox import Sandbox, SandboxError

logger = logging.getLogger("filebridge.agent.upload")

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5

# 这些状态码重试没有意义：请求本身就不对，重发只会再错一次
NON_RETRYABLE = {400, 401, 403, 404, 409, 411, 413, 507}

Notify = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class UploadJob:
    upload_id: str
    rel: str
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    resume: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UploadJob:
        upload_id = payload.get("upload_id")
        rel = payload.get("rel")
        if not isinstance(upload_id, str) or not upload_id.isalnum():
            raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "upload_id 不合法")
        if not isinstance(rel, str) or not rel:
            raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "rel 不合法")

        try:
            total_size = int(payload.get("total_size"))
            chunk_size = int(payload.get("chunk_size"))
            total_chunks = int(payload.get("total_chunks"))
        except (TypeError, ValueError) as exc:
            raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "分块参数不是整数") from exc

        if total_size < 0 or chunk_size <= 0 or total_chunks < 0:
            raise AgentCommandError(ErrorCode.INVALID_ARGUMENTS, "分块参数越界")
        expected_chunks = math.ceil(total_size / chunk_size) if total_size else 0
        if total_chunks != expected_chunks:
            raise AgentCommandError(
                ErrorCode.INVALID_ARGUMENTS,
                f"total_chunks 应为 {expected_chunks}，收到 {total_chunks}",
            )

        return cls(
            upload_id=upload_id,
            rel=rel,
            filename=str(payload.get("filename") or rel),
            total_size=total_size,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            resume=bool(payload.get("resume", False)),
        )


def _read_at(handle: Any, offset: int, length: int) -> bytes:
    """在线程里读一块。显式 seek，不依赖文件指针的隐含状态。"""
    handle.seek(offset)
    return handle.read(length)


class UploadManager:
    def __init__(
        self,
        sandbox: Sandbox,
        config: AgentConfig,
        notify: Notify,
    ) -> None:
        self.sandbox = sandbox
        self.config = config
        self.notify = notify
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    @property
    def active(self) -> int:
        return sum(1 for task in self._jobs.values() if not task.done())

    # --- 生命周期 -------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.http_base,
                headers={"Authorization": f"Bearer {self.config.token}"},
                timeout=httpx.Timeout(self.config.http_timeout, connect=self.config.open_timeout),
                verify=self.config.verify_tls,
                follow_redirects=False,
            )
        return self._client

    async def start(self, job: UploadJob) -> None:
        async with self._lock:
            running = self._jobs.get(job.upload_id)
            if running is not None and not running.done():
                logger.info("upload %s already running, ignore duplicate start", job.upload_id)
                return
            self._cancelled.discard(job.upload_id)
            task = asyncio.create_task(
                self._run(job), name=f"upload-{job.upload_id[:8]}"
            )
            self._jobs[job.upload_id] = task

    async def cancel(self, upload_id: str) -> None:
        self._cancelled.add(upload_id)
        task = self._jobs.get(upload_id)
        if task is not None and not task.done():
            task.cancel()

    async def close(self) -> None:
        for task in list(self._jobs.values()):
            task.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- 主流程 ---------------------------------------------------------

    async def _run(self, job: UploadJob) -> None:
        try:
            await self._upload(job)
        except asyncio.CancelledError:
            # A 主动取消的，不需要回报；本地被中断的，让它自己发现空闲超时
            raise
        except UploadCancelled:
            logger.info("upload %s was cancelled by server", job.upload_id)
        except UploadFailed as exc:
            logger.warning("upload %s failed: %s", job.upload_id, exc)
            await self._report_failure(job, exc.code, exc.message)
        except Exception as exc:
            logger.exception("upload %s crashed", job.upload_id)
            await self._report_failure(job, ErrorCode.INTERNAL, str(exc))
        finally:
            self._jobs.pop(job.upload_id, None)

    async def _upload(self, job: UploadJob) -> None:
        if job.upload_id in self._cancelled:
            raise UploadCancelled

        try:
            path = await asyncio.to_thread(self.sandbox.resolve, job.rel)
        except SandboxError as exc:
            raise UploadFailed(ErrorCode.FORBIDDEN, str(exc)) from exc

        status = await self._fetch_status(job)
        received = {int(i) for i in status.get("received") or []}

        try:
            before = await asyncio.to_thread(path.stat)
        except FileNotFoundError as exc:
            raise UploadFailed(ErrorCode.FILE_NOT_FOUND, f"文件不存在：{job.rel}") from exc

        if before.st_size != job.total_size:
            raise UploadFailed(
                ErrorCode.SIZE_CHANGED,
                f"文件大小已变化：任务创建时 {job.total_size}，现在 {before.st_size}",
            )

        digest = hashlib.sha256()
        handle = await asyncio.to_thread(open, path, "rb")
        try:
            pending: list[tuple[int, bytes]] = []
            for index in range(job.total_chunks):
                if job.upload_id in self._cancelled:
                    raise UploadCancelled

                offset = index * job.chunk_size
                expected = min(job.chunk_size, job.total_size - offset)
                chunk = await asyncio.to_thread(_read_at, handle, offset, expected)

                if len(chunk) != expected:
                    # 文件在上传过程中被截断
                    raise UploadFailed(
                        ErrorCode.SIZE_CHANGED,
                        f"读到第 {index} 块时只有 {len(chunk)} 字节，期望 {expected}",
                    )

                digest.update(chunk)

                if index in received:
                    continue

                pending.append((index, chunk))
                if len(pending) >= self.config.max_concurrent_uploads:
                    await self._send_batch(job, pending)
                    pending = []

            if pending:
                await self._send_batch(job, pending)
        finally:
            await asyncio.to_thread(handle.close)

        try:
            after = await asyncio.to_thread(path.stat)
        except FileNotFoundError as exc:
            raise UploadFailed(ErrorCode.SIZE_CHANGED, f"文件在上传过程中消失：{job.rel}") from exc

        if after.st_size != before.st_size or after.st_mtime != before.st_mtime:
            # 日志文件、正在写的数据库转储都会这样。宁可整体失败，
            # 也不要把半个文件当成完整的交上去。
            raise UploadFailed(
                ErrorCode.SIZE_CHANGED,
                f"文件在上传过程中被修改：{before.st_size} → {after.st_size} 字节",
            )

        await self._complete(job, digest.hexdigest())

    # --- HTTP -----------------------------------------------------------

    async def _fetch_status(self, job: UploadJob) -> dict[str, Any]:
        response = await self._request("GET", f"/internal/uploads/{job.upload_id}/status")
        if response.status_code == 404:
            raise UploadCancelled
        self._raise_for_status(response, "查询上传状态")
        return dict(response.json())

    async def _send_batch(self, job: UploadJob, batch: list[tuple[int, bytes]]) -> None:
        await asyncio.gather(*(self._put_chunk(job, index, chunk) for index, chunk in batch))

    async def _put_chunk(self, job: UploadJob, index: int, chunk: bytes) -> None:
        url = f"/internal/uploads/{job.upload_id}/chunks/{index}"
        last_error: object = None

        for attempt in range(RETRY_ATTEMPTS):
            if job.upload_id in self._cancelled:
                raise UploadCancelled
            try:
                response = await self._request("PUT", url, content=chunk)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    return
                if response.status_code in NON_RETRYABLE:
                    code, message = _error_of(response)
                    raise UploadFailed(code, f"第 {index} 块被拒绝：{message}")
                last_error = f"HTTP {response.status_code}"

            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt))

        raise UploadFailed(
            ErrorCode.INTERNAL, f"第 {index} 块重试 {RETRY_ATTEMPTS} 次仍失败：{last_error}"
        )

    async def _complete(self, job: UploadJob, sha256_hex: str) -> None:
        response = await self._request(
            "POST", f"/internal/uploads/{job.upload_id}/complete", json={"sha256": sha256_hex}
        )
        if response.status_code == 404:
            raise UploadCancelled
        self._raise_for_status(response, "提交完成")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http().request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise UploadFailed(
                ErrorCode.INTERNAL, f"{method} {url} 失败：{exc}"
            ) from exc

    def _raise_for_status(self, response: httpx.Response, what: str) -> None:
        if response.status_code < 400:
            return
        code, message = _error_of(response)
        raise UploadFailed(code, f"{what}失败（HTTP {response.status_code}）：{message}")

    async def _report_failure(self, job: UploadJob, code: ErrorCode, message: str) -> None:
        if job.upload_id in self._cancelled:
            return
        try:
            await self.notify(
                {
                    "kind": "upload_failed",
                    "upload_id": job.upload_id,
                    "code": code.value,
                    "message": message,
                }
            )
        except Exception:  # noqa: BLE001 - 回报失败本身失败，只能记日志
            logger.info("could not report failure for %s (connection lost)", job.upload_id)


def _error_of(response: httpx.Response) -> tuple[ErrorCode, str]:
    """把 A 的错误体翻回 code。翻不出来就给 internal + 原始文本。"""
    try:
        body = response.json()
    except ValueError:
        return ErrorCode.INTERNAL, response.text[:200] or f"HTTP {response.status_code}"
    code = body.get("code")
    message = str(body.get("message") or body.get("detail") or "")
    try:
        return ErrorCode(code), message
    except ValueError:
        return ErrorCode.INTERNAL, message or f"HTTP {response.status_code}"
