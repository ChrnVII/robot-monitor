"""任务编排。

一次取件的完整时序（进度只由 A 记录，B 不上报进度）：

    C: POST /tasks
    A: 建 task(pending) → stat_file → 校验大小/磁盘 → 建 upload + 预分配 staging
       → start_upload(agent 立刻 ack，真正传在 B 的后台任务里)
    B: PUT /internal/uploads/{id}/chunks/{n}  × N（A 记 received，进度即真相）
    B: POST /internal/uploads/{id}/complete  {sha256}
    A: 重算 sha256 → 原子改名 → files 行 → task done → SSE

关键不变量：**任何一步失败都要把 task 和 upload 一起落到终态**，不能留下
"任务在转圈但 B 早就不传了"的僵尸行。所有失败路径都走 `_fail_upload`。
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from typing import Any

from .auth import AuthService
from .config import Settings
from .db import Database
from .errors import ApiError
from .events import EventBus
from .hub import AgentError, Hub
from .paths import basename_of, normalize_client_path
from .protocol import CommandName, ErrorCode
from .storage import Storage, StorageError

logger = logging.getLogger("filebridge.tasks")

# 任务状态机
PENDING = "pending"
UPLOADING = "uploading"
VERIFYING = "verifying"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"

TERMINAL = {DONE, FAILED, CANCELED}


def _chunk_count(total_size: int, chunk_size: int) -> int:
    if total_size <= 0:
        return 0
    return math.ceil(total_size / chunk_size)


class TaskService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        hub: Hub,
        storage: Storage,
        bus: EventBus,
        auth: AuthService,
    ) -> None:
        self.db = db
        self.settings = settings
        self.hub = hub
        self.storage = storage
        self.bus = bus
        self.auth = auth

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_tasks(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        rows = await self.db.fetchall(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        total = await self.db.fetchval("SELECT COUNT(*) FROM tasks", default=0)
        return {"items": [self._task_view(r) for r in rows], "total": int(total)}

    async def get_task(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, f"task {task_id} not found")
        return self._task_view(row)

    async def get_upload(self, upload_id: str) -> Any:
        return await self.db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))

    # ------------------------------------------------------------------
    # 建任务
    # ------------------------------------------------------------------

    async def create_task(self, agent_id: str, path: str, *, actor: str = "admin") -> dict[str, Any]:
        try:
            rel = normalize_client_path(path)
        except ValueError as exc:
            raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, str(exc)) from exc

        if not rel:
            raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, "请选择具体文件，根目录不能作为取件对象")
        if not self.hub.is_online(agent_id):
            raise ApiError(409, ErrorCode.AGENT_OFFLINE, "节点不在线，无法下发取件任务")

        task_id = uuid.uuid4().hex[:16]
        now = time.time()
        await self.db.execute(
            "INSERT INTO tasks (id, agent_id, type, src_path, status, created_at, updated_at)"
            " VALUES (?, ?, 'fetch', ?, ?, ?, ?)",
            (task_id, agent_id, rel, PENDING, now, now),
        )
        self._publish_task(task_id, extra={"agent_id": agent_id})

        # ① 先 stat：确认存在、是文件、有多大。不做这一步就派上传，
        #    错误会在 B 侧以各种奇怪形式出现（读不到、读到目录、大小对不上）。
        try:
            stat = await self.hub.request(
                agent_id, CommandName.STAT_FILE, {"rel": rel}, timeout=self.settings.command_timeout_seconds
            )
        except AgentError as exc:
            await self._mark_task_failed(task_id, exc.code, exc.message)
            raise self._api_from_agent(exc) from exc

        if stat.get("is_dir"):
            message = "这是目录，只能选择文件"
            await self._mark_task_failed(task_id, ErrorCode.NOT_A_FILE, message)
            raise ApiError(400, ErrorCode.NOT_A_FILE, message)

        total_size = int(stat.get("size") or 0)
        filename = str(stat.get("name") or basename_of(rel))
        if total_size > self.settings.max_file_bytes:
            message = f"文件 {total_size} 字节，超过单文件上限 {self.settings.max_file_bytes}"
            await self._mark_task_failed(task_id, ErrorCode.INVALID_ARGUMENTS, message)
            raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, message)

        # ② 磁盘预检：宁可建任务时直接拒绝，也不要传到 90% 再说放不下
        try:
            self.storage.ensure_capacity(total_size)
        except StorageError as exc:
            await self._mark_task_failed(task_id, exc.code, exc.message)
            raise ApiError(507, exc.code, exc.message) from exc

        chunk_size = self.settings.chunk_size_for(total_size)
        total_chunks = _chunk_count(total_size, chunk_size)
        upload_id = uuid.uuid4().hex
        await self.storage.create_staging(upload_id, total_size)

        await self.db.execute(
            "INSERT INTO uploads (id, task_id, agent_id, filename, rel_path, total_size,"
            " chunk_size, total_chunks, received_json, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)",
            (
                upload_id,
                task_id,
                agent_id,
                filename,
                rel,
                total_size,
                chunk_size,
                total_chunks,
                UPLOADING,
                now,
                now,
            ),
        )
        await self.db.execute(
            "UPDATE tasks SET status = ?, upload_id = ?, total_bytes = ?, updated_at = ? WHERE id = ?",
            (UPLOADING, upload_id, total_size, time.time(), task_id),
        )

        payload = {
            "upload_id": upload_id,
            "rel": rel,
            "filename": filename,
            "total_size": total_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
        }
        try:
            await self.hub.request(
                agent_id, CommandName.START_UPLOAD, payload, timeout=self.settings.command_timeout_seconds
            )
        except AgentError as exc:
            await self._fail_upload(upload_id, exc.code, exc.message)
            raise self._api_from_agent(exc) from exc

        await self.auth.audit(actor, "task.create", target=task_id, detail=f"{agent_id}:{rel}")
        self._publish_task(task_id, extra={"agent_id": agent_id, "filename": filename})
        logger.info("task %s created for %s:%s (%d bytes)", task_id, agent_id, rel, total_size)
        return await self.get_task(task_id)

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    async def cancel_task(self, task_id: str, *, actor: str = "admin") -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, f"task {task_id} not found")
        if row["status"] in TERMINAL:
            return self._task_view(row)

        upload_id = row["upload_id"]
        if upload_id and self.hub.is_online(row["agent_id"]):
            # 尽力而为：B 不在线也无所谓，它重连后会从 A 的 pending 列表里
            # 发现这个 upload 已经不存在，自然不再续传。
            try:
                await self.hub.request(
                    row["agent_id"],
                    CommandName.CANCEL_TASK,
                    {"upload_id": upload_id},
                    timeout=5.0,
                )
            except AgentError as exc:
                logger.info("cancel notice for %s not delivered: %s", upload_id, exc)

        now = time.time()
        await self.db.execute(
            "UPDATE tasks SET status = ?, error_code = ?, error = ?, updated_at = ? WHERE id = ?",
            (CANCELED, ErrorCode.INVALID_ARGUMENTS.value, "已取消", now, task_id),
        )
        if upload_id:
            await self.db.execute(
                "UPDATE uploads SET status = ?, updated_at = ? WHERE id = ?",
                (CANCELED, now, upload_id),
            )
            try:
                await self.storage.drop_staging(upload_id)
            except StorageError:
                pass
        self._publish_task(task_id)
        await self.auth.audit(actor, "task.cancel", target=task_id)
        return await self.get_task(task_id)

    # ------------------------------------------------------------------
    # 分块
    # ------------------------------------------------------------------

    async def add_chunk(self, upload_id: str, index: int, data: bytes) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, f"upload {upload_id} not found")
        if row["status"] != UPLOADING:
            raise ApiError(409, ErrorCode.INVALID_ARGUMENTS, f"upload is {row['status']}")

        total_size = int(row["total_size"])
        chunk_size = int(row["chunk_size"])
        total_chunks = int(row["total_chunks"])

        if index < 0 or index >= max(total_chunks, 1):
            raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, f"chunk index {index} out of range")

        offset = index * chunk_size
        expected = min(chunk_size, total_size - offset)
        if len(data) != expected:
            raise ApiError(
                400,
                ErrorCode.INVALID_ARGUMENTS,
                f"chunk {index} should be {expected} bytes, got {len(data)}",
            )

        try:
            await self.storage.write_chunk(upload_id, offset, data, total_size)
        except StorageError as exc:
            raise ApiError(400, exc.code, exc.message) from exc

        # 块号合并必须原子：两个块并发到达时后写的会覆盖先写的
        async with self.db.write_transaction() as conn:
            fresh = await (await conn.execute(
                "SELECT received_json FROM uploads WHERE id = ?", (upload_id,)
            )).fetchone()
            received = set(json.loads(fresh["received_json"]))
            received.add(index)
            now = time.time()
            await conn.execute(
                "UPDATE uploads SET received_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(sorted(received)), now, upload_id),
            )
            received_count = len(received)
            await conn.execute(
                "UPDATE tasks SET transferred_bytes = ?, updated_at = ? WHERE id = ?",
                (
                    min(total_size, received_count * chunk_size),
                    now,
                    row["task_id"],
                ),
            )

        self._publish_progress(row["task_id"], upload_id, received_count, total_chunks, total_size, chunk_size)
        return {
            "upload_id": upload_id,
            "received": received_count,
            "total_chunks": total_chunks,
        }

    async def upload_status(self, upload_id: str) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, f"upload {upload_id} not found")
        return {
            "upload_id": upload_id,
            "status": row["status"],
            "chunk_size": row["chunk_size"],
            "total_chunks": row["total_chunks"],
            "total_size": row["total_size"],
            "received": json.loads(row["received_json"]),
        }

    # ------------------------------------------------------------------
    # 完成
    # ------------------------------------------------------------------

    async def complete_upload(self, upload_id: str, sha256_reported: str | None) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, f"upload {upload_id} not found")
        if row["status"] == DONE:
            return await self.get_task(row["task_id"])
        if row["status"] != UPLOADING:
            raise ApiError(409, ErrorCode.INVALID_ARGUMENTS, f"upload is {row['status']}")

        received = set(json.loads(row["received_json"]))
        total_chunks = int(row["total_chunks"])
        missing = sorted(set(range(total_chunks)) - received)
        if missing:
            preview = ", ".join(str(n) for n in missing[:10])
            raise ApiError(
                400,
                ErrorCode.INVALID_ARGUMENTS,
                f"{len(missing)} 个块缺失（前几个：{preview}），不能完成",
            )

        await self.db.execute(
            "UPDATE uploads SET status = ?, sha256_expected = ?, updated_at = ? WHERE id = ?",
            (VERIFYING, sha256_reported, time.time(), upload_id),
        )

        file_id = uuid.uuid4().hex
        try:
            actual = await self.storage.finalize(
                upload_id, row["agent_id"], file_id, expected_sha256=sha256_reported
            )
        except StorageError as exc:
            # 校验失败保留 staging 供排查；其余错误清理掉
            keep = exc.code is ErrorCode.CHECKSUM_MISMATCH
            await self._fail_upload(upload_id, exc.code, exc.message, keep_staging=keep)
            raise ApiError(409 if keep else 400, exc.code, exc.message) from exc

        now = time.time()
        stored_path = str(self.storage.final_path(row["agent_id"], file_id).relative_to(self.settings.storage_dir))
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO files (id, agent_id, orig_name, rel_path, size, sha256, stored_path,"
                " created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    file_id,
                    row["agent_id"],
                    row["filename"],
                    row["rel_path"],
                    row["total_size"],
                    actual,
                    stored_path,
                    now,
                ),
            )
            await conn.execute(
                "UPDATE uploads SET status = ?, updated_at = ? WHERE id = ?", (DONE, now, upload_id)
            )
            await conn.execute(
                "UPDATE tasks SET status = ?, file_id = ?, transferred_bytes = total_bytes,"
                " updated_at = ? WHERE id = ?",
                (DONE, file_id, now, row["task_id"]),
            )

        await self.auth.audit(
            "agent", "file.store", target=file_id, detail=f"{row['filename']} ({row['total_size']} bytes)"
        )
        self.bus.publish(
            "file_added",
            {
                "file_id": file_id,
                "name": row["filename"],
                "size": row["total_size"],
                "sha256": actual,
            },
        )
        self._publish_task(row["task_id"], extra={"file_id": file_id})
        logger.info("upload %s committed as file %s (%s)", upload_id, file_id, row["filename"])
        return await self.get_task(row["task_id"])

    # ------------------------------------------------------------------
    # 失败与续传
    # ------------------------------------------------------------------

    async def _fail_upload(
        self, upload_id: str, code: ErrorCode, message: str, *, keep_staging: bool = False
    ) -> None:
        row = await self.db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
        if row is None:
            return
        now = time.time()
        await self.db.execute(
            "UPDATE uploads SET status = ?, updated_at = ? WHERE id = ?", (FAILED, now, upload_id)
        )
        await self.db.execute(
            "UPDATE tasks SET status = ?, error_code = ?, error = ?, updated_at = ? WHERE id = ?",
            (FAILED, code.value, message, now, row["task_id"]),
        )
        if not keep_staging:
            try:
                await self.storage.drop_staging(upload_id)
            except StorageError:
                pass
        self._publish_task(row["task_id"], extra={"error": message})

    async def _mark_task_failed(self, task_id: str, code: ErrorCode, message: str) -> None:
        now = time.time()
        await self.db.execute(
            "UPDATE tasks SET status = ?, error_code = ?, error = ?, updated_at = ? WHERE id = ?",
            (FAILED, code.value, message, now, task_id),
        )
        self._publish_task(task_id, extra={"error": message})

    async def pending_uploads(self, agent_id: str) -> list[dict[str, Any]]:
        """agent 连上时告诉它"这些还没传完"，让它自己续。

        幂等的 PUT 让续传不需要任何协商：B 从第一个缺失块继续，重复上传的块
        直接覆盖，代价只有一点流量。
        """
        rows = await self.db.fetchall(
            "SELECT * FROM uploads WHERE agent_id = ? AND status = ? ORDER BY created_at",
            (agent_id, UPLOADING),
        )
        return [
            {
                "upload_id": r["id"],
                "rel": r["rel_path"],
                "filename": r["filename"],
                "total_size": r["total_size"],
                "chunk_size": r["chunk_size"],
                "total_chunks": r["total_chunks"],
                "resume": True,
            }
            for r in rows
        ]

    async def handle_agent_event(self, agent_id: str, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "upload_failed":
            upload_id = payload.get("upload_id")
            if not upload_id:
                return
            row = await self.db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
            if row is None or row["agent_id"] != agent_id or row["status"] != UPLOADING:
                return
            code = payload.get("code") or ErrorCode.INTERNAL.value
            try:
                error_code = ErrorCode(code)
            except ValueError:
                error_code = ErrorCode.INTERNAL
            await self._fail_upload(
                upload_id, error_code, str(payload.get("message") or "agent reported failure")
            )
        elif kind == "heartbeat":
            return
        else:
            logger.debug("ignored agent event %r", kind)

    async def recover_after_restart(self) -> None:
        """进程重启后的收尾。

        - `verifying` 说明上次在"校验中"被打断：改回 `uploading`，让 agent
          重连后续传（块是幂等的，重复校验没有副作用）
        - `pending` 的任务从来没有真正开始：直接失败
        - 孤儿 staging（没有对应 upload 行的 .part）删掉，否则一直占盘
        """
        rows = await self.db.fetchall("SELECT id FROM uploads WHERE status = ?", (VERIFYING,))
        for row in rows:
            await self.db.execute(
                "UPDATE uploads SET status = ? WHERE id = ?", (UPLOADING, row["id"])
            )

        await self.db.execute(
            "UPDATE tasks SET status = ?, error_code = ?, error = ?, updated_at = ? WHERE status = ?",
            (FAILED, ErrorCode.INTERNAL.value, "服务重启时任务尚未开始", time.time(), PENDING),
        )

        live = await self.db.fetchall("SELECT id FROM uploads WHERE status = ?", (UPLOADING,))
        keep = {row["id"] for row in live}
        removed = await self.storage.sweep_orphan_staging(keep)
        if removed:
            logger.info("removed %d orphan staging file(s)", len(removed))

    async def sweep_idle_uploads(self) -> int:
        """空闲超时的上传会话回收。B 断线后没人续传的，30 分钟就清掉。"""
        cutoff = time.time() - self.settings.upload_idle_minutes * 60
        rows = await self.db.fetchall(
            "SELECT id FROM uploads WHERE status = ? AND updated_at < ?", (UPLOADING, cutoff)
        )
        for row in rows:
            await self._fail_upload(
                row["id"], ErrorCode.TIMEOUT, "上传会话空闲超时，已回收"
            )
        return len(rows)

    # ------------------------------------------------------------------
    # 文件库
    # ------------------------------------------------------------------

    async def list_files(
        self,
        *,
        query: str | None = None,
        deleted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses = ["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"]
        params: list[Any] = []
        if query:
            clauses.append("orig_name LIKE ?")
            params.append(f"%{query}%")
        where = " AND ".join(clauses)
        rows = await self.db.fetchall(
            f"SELECT * FROM files WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        total = await self.db.fetchval(f"SELECT COUNT(*) FROM files WHERE {where}", params, default=0)
        return {
            "items": [self._file_view(r) for r in rows],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }

    async def soft_delete(self, file_id: str, *, actor: str = "admin") -> None:
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None or row["deleted_at"] is not None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "文件不存在或已在回收站")
        await self.storage.move_to_trash(row["agent_id"], file_id)
        await self.db.execute(
            "UPDATE files SET deleted_at = ? WHERE id = ?", (time.time(), file_id)
        )
        await self.auth.audit(actor, "file.delete", target=file_id, detail=row["orig_name"])
        self.bus.publish("file_removed", {"file_id": file_id})

    async def restore(self, file_id: str, *, actor: str = "admin") -> None:
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None or row["deleted_at"] is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "回收站里没有这个文件")
        await self.storage.restore_from_trash(row["agent_id"], file_id)
        await self.db.execute("UPDATE files SET deleted_at = NULL WHERE id = ?", (file_id,))
        await self.auth.audit(actor, "file.restore", target=file_id, detail=row["orig_name"])
        self.bus.publish("file_restored", {"file_id": file_id})

    async def purge(self, file_id: str, *, actor: str = "admin") -> None:
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "文件不存在")
        if row["deleted_at"] is None:
            raise ApiError(409, ErrorCode.INVALID_ARGUMENTS, "只能彻底删除回收站里的文件")
        await self.storage.purge(file_id)
        await self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        await self.auth.audit(actor, "file.purge", target=file_id, detail=row["orig_name"])

    async def purge_expired_trash(self) -> int:
        cutoff = time.time() - self.settings.trash_retention_days * 86400
        rows = await self.db.fetchall(
            "SELECT id FROM files WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
        )
        for row in rows:
            await self.purge(row["id"], actor="system")
        return len(rows)

    async def storage_path_for(self, file_id: str) -> tuple[Any, Any]:
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None or row["deleted_at"] is not None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "文件不存在")
        return row, self.storage.final_path(row["agent_id"], file_id)

    # ------------------------------------------------------------------
    # 视图与小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _api_from_agent(error: AgentError) -> ApiError:
        from .errors import from_agent_error

        return from_agent_error(error)

    def _task_view(self, row: Any) -> dict[str, Any]:
        total = row["total_bytes"] or 0
        transferred = row["transferred_bytes"] or 0
        progress = 100.0 if row["status"] == DONE else (transferred / total * 100 if total else 0.0)
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "type": row["type"],
            "path": row["src_path"],
            "name": basename_of(row["src_path"]) or row["src_path"],
            "status": row["status"],
            "file_id": row["file_id"],
            "upload_id": row["upload_id"],
            "total_bytes": total,
            "transferred_bytes": total if row["status"] == DONE else transferred,
            "progress": round(min(progress, 100.0), 2),
            "error": row["error"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _file_view(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "name": row["orig_name"],
            "rel_path": row["rel_path"],
            "size": row["size"],
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "deleted_at": row["deleted_at"],
        }

    def _publish_task(self, task_id: str, extra: dict[str, Any] | None = None) -> None:
        row_task = {"task_id": task_id}
        if extra:
            row_task.update(extra)
        self.bus.publish("task_update", row_task)

    def _publish_progress(
        self,
        task_id: str,
        upload_id: str,
        received: int,
        total_chunks: int,
        total_size: int,
        chunk_size: int,
    ) -> None:
        percent = 100.0 if total_chunks == 0 else received / total_chunks * 100
        self.bus.publish(
            "task_progress",
            {
                "task_id": task_id,
                "upload_id": upload_id,
                "received": received,
                "total_chunks": total_chunks,
                "bytes": min(total_size, received * chunk_size),
                "total_bytes": total_size,
                "progress": round(percent, 2),
            },
        )
