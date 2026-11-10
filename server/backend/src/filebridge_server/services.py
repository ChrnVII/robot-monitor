"""任务编排。

一次取件的完整时序（进度只由 A 记录，B 不上报进度）：

    C: POST /tasks
    A: 建 task → stat_file → 查库里有没有同一份内容（命中就不传）
       → 未命中：校验磁盘 → 建 upload + 预分配 staging → start_upload
    B: PUT /internal/uploads/{id}/chunks/{n}  × N（A 记 received，进度即真相）
    B: POST /internal/uploads/{id}/complete  {sha256}
    A: 重算 sha256 → 判重/替换/落盘 → files 行 → task done → SSE

落库规则：**路径是身份，内容是存储**。同路径同内容跳过，同路径不同内容替换，
不同路径同内容硬链接复用。

关键不变量：**任何一步失败都要把 task 和 upload 一起落到终态**，不能留下
"任务在转圈但 B 早就不传了"的僵尸行。所有失败路径都走 `_fail_upload`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .auth import AuthService
from .config import Settings
from .db import Database
from .errors import ApiError, from_agent_error
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
# 内容与库里已有文件完全一致：既没新增文件行，也没占新磁盘。
# 单独一个终态而不是复用 done——"完成了"和"什么都没做"对用户是两件事。
DEDUPED = "deduped"
FAILED = "failed"
CANCELED = "canceled"

TERMINAL = {DONE, DEDUPED, FAILED, CANCELED}

# 判重前让 B 算一次 sha256 的超时。按 50MB/s 的保守读盘速度估，再加固定余量：
# 估小了会把慢盘机器误判成"算不出来"，然后白传一遍——那正是要避免的事。
# 上限 240s 压在两处超时之下：nginx 对 /api/ 的 proxy_read_timeout 是 300s，
# 前端 axios 是 30s（所以单文件取件还另有一道更短的上限，见 routes/tasks.py）。
HASH_BYTES_PER_SECOND = 50 * 1024 * 1024
HASH_BASE_TIMEOUT = 20.0
HASH_MAX_TIMEOUT = 240.0


def _chunk_count(total_size: int, chunk_size: int) -> int:
    if total_size <= 0:
        return 0
    return math.ceil(total_size / chunk_size)


def _clean_prefix(path: str) -> str:
    """目录前缀统一走一遍路径规整，顺带挡掉 `..` 之类的东西。"""
    try:
        return normalize_client_path(path)
    except ValueError as exc:
        raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, str(exc)) from exc


def _prefix_like(prefix: str) -> str:
    if not prefix:
        return "%"
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


def _join_path(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


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
        # 后台批量建任务的强引用：asyncio 只保留弱引用，不留住会被 GC 掉
        self._batch_jobs: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_tasks(
        self, *, agent_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        # 顶栏选了哪台节点，任务页就只看那台，不混在一起
        where = "WHERE agent_id = ?" if agent_id else ""
        params: list[Any] = [agent_id] if agent_id else []
        rows = await self.db.fetchall(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        total = await self.db.fetchval(f"SELECT COUNT(*) FROM tasks {where}", params, default=0)
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

    async def create_task(
        self,
        agent_id: str,
        path: str,
        *,
        actor: str = "admin",
        hash_timeout: float | None = None,
    ) -> dict[str, Any]:
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
            raise from_agent_error(exc) from exc

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

        # ② 上传前判重：先比大小，再让 B 算一次哈希。命中就不传字节。
        found = await self._lookup_before_upload(
            agent_id, rel, total_size, max_wait=hash_timeout
        )
        if found is not None:
            kind, source = found
            if kind == "skipped":
                return await self._mark_task_deduped(task_id, source, actor=actor)
            return await self._store_linked(
                task_id,
                agent_id=agent_id,
                rel=rel,
                filename=filename,
                size=total_size,
                source=source,
                actor=actor,
            )

        # ③ 磁盘预检：宁可建任务时直接拒绝，也不要传到 90% 再说放不下
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
            raise from_agent_error(exc) from exc

        await self.auth.audit(actor, "task.create", target=task_id, detail=f"{agent_id}:{rel}")
        self._publish_task(task_id, extra={"agent_id": agent_id, "filename": filename})
        logger.info("task %s created for %s:%s (%d bytes)", task_id, agent_id, rel, total_size)
        return await self.get_task(task_id)

    # ------------------------------------------------------------------
    # 内容判重与落库
    #
    # 两条规则，各管一件事：
    #   记录按路径 —— 一个路径在库里只有一条，树才和远端目录长得一样；
    #   磁盘按内容 —— sha 相同就硬链接，实际只占一份。
    # 于是"重复"分两种：同路径同内容 = 什么都不用做；换了路径的同内容 = 不传字节，
    # 但要在新路径上建一条记录，指向同一份数据。
    # ------------------------------------------------------------------

    async def _file_at(self, agent_id: str, rel: str) -> Any | None:
        """这个路径在库里已有的那条（未删除）。老数据 rel_path 可能为空，用名字兜底。"""
        return await self.db.fetchone(
            "SELECT * FROM files WHERE agent_id = ? AND COALESCE(rel_path, orig_name) = ?",
            (agent_id, rel),
        )

    async def _file_with_sha(self, agent_id: str, digest: str) -> Any | None:
        return await self.db.fetchone(
            "SELECT * FROM files WHERE agent_id = ? AND sha256 = ?"
            " ORDER BY created_at LIMIT 1",
            (agent_id, digest),
        )

    async def _lookup_before_upload(
        self, agent_id: str, rel: str, total_size: int, *, max_wait: float | None = None
    ) -> tuple[str, Any] | None:
        """上传前查一次。返回 ("skipped", 同路径的那条) / ("linked", 同内容的另一条) / None。"""
        # 先比大小：绝大多数文件大小唯一，这一步就把它们挡在"读盘算哈希"之外。
        candidate = await self.db.fetchone(
            "SELECT 1 AS hit FROM files WHERE agent_id = ? AND size = ? LIMIT 1",
            (agent_id, total_size),
        )
        if candidate is None:
            return None

        budget = self._hash_timeout(total_size)
        if max_wait is not None:
            budget = min(budget, max_wait)

        try:
            result = await self.hub.request(
                agent_id, CommandName.HASH_FILE, {"rel": rel}, timeout=budget
            )
        except AgentError as exc:
            # 老版本 agent 回 unknown_command、慢盘读超时。这只是一次省掉整份上传的
            # 机会，抓不住就照常传，不能拦路。
            logger.info("pre-hash skipped for %s: %s", rel, exc)
            return None

        digest = str(result.get("sha256") or "").lower()
        if len(digest) != 64:
            logger.warning("agent returned a malformed sha256 for %s: %r", rel, digest)
            return None

        same = await self._file_at(agent_id, rel)
        if same is not None and same["sha256"] == digest:
            return ("skipped", same)

        twin = await self._file_with_sha(agent_id, digest)
        if twin is not None:
            return ("linked", twin)
        return None

    async def _store_linked(
        self,
        task_id: str,
        *,
        agent_id: str,
        rel: str,
        filename: str,
        size: int,
        source: Any,
        upload_id: str | None = None,
        transferred: int = 0,
        actor: str = "admin",
    ) -> dict[str, Any]:
        """内容已经在库里：不传字节，只在新路径上建（或替换）一条记录。"""
        same = await self._file_at(agent_id, rel)
        now = time.time()

        if same is not None:
            # 同一路径、内容变了：换的是目录项，不是 inode 里的字节，
            # 所以硬链接到别处的同内容副本不会被这次替换污染。
            if upload_id is not None:
                await self.storage.commit_over(upload_id, same["stored_path"])
            elif not await self.storage.link_over(source["stored_path"], same["stored_path"]):
                logger.info("hardlink unavailable, fell back to copy for %s:%s", agent_id, rel)
            await self.db.execute(
                "UPDATE files SET sha256 = ?, size = ?, created_at = ? WHERE id = ?",
                (source["sha256"], size, now, same["id"]),
            )
            file_id = same["id"]
            outcome = "replaced"
        else:
            file_id = uuid.uuid4().hex
            if not await self.storage.link_existing(source["stored_path"], agent_id, file_id):
                logger.info("hardlink unavailable, fell back to copy for %s:%s", agent_id, rel)
            stored_path = str(
                self.storage.final_path(agent_id, file_id).relative_to(self.settings.storage_dir)
            )
            await self.db.execute(
                "INSERT INTO files (id, agent_id, orig_name, rel_path, size, sha256, stored_path,"
                " created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (file_id, agent_id, filename, rel, size, source["sha256"], stored_path, now),
            )
            outcome = "linked"

        await self.db.execute(
            "UPDATE tasks SET status = ?, file_id = ?, transferred_bytes = ?, updated_at = ?"
            " WHERE id = ?",
            (DONE, file_id, transferred, now, task_id),
        )
        if upload_id is not None:
            await self.storage.drop_staging(upload_id)
            await self.db.execute(
                "UPDATE uploads SET status = ?, updated_at = ? WHERE id = ?", (DONE, now, upload_id)
            )

        await self.auth.audit(actor, f"file.{outcome}", target=file_id, detail=f"{agent_id}:{rel}")
        self.bus.publish("files_changed", {"agent_id": agent_id, "path": rel})
        self._publish_task(task_id, extra={"file_id": file_id})
        logger.info("task %s %s into %s from file %s", task_id, outcome, rel, source["id"])
        return await self.get_task(task_id)

    async def _mark_task_deduped(
        self,
        task_id: str,
        existing: Any,
        *,
        upload_id: str | None = None,
        transferred: int = 0,
        actor: str = "admin",
    ) -> dict[str, Any]:
        """同路径、同内容：任务落 deduped——不新增行，也不占新磁盘。

        `file_id` 指回已有那条，任务页仍然说得清"这份内容对应哪个文件"。
        """
        now = time.time()
        async with self.db.write_transaction() as conn:
            await conn.execute(
                "UPDATE tasks SET status = ?, file_id = ?, transferred_bytes = ?, updated_at = ?"
                " WHERE id = ?",
                (DEDUPED, existing["id"], transferred, now, task_id),
            )
            if upload_id is not None:
                await conn.execute(
                    "UPDATE uploads SET status = ?, updated_at = ? WHERE id = ?",
                    (DONE, now, upload_id),
                )

        await self.auth.audit(
            actor, "task.dedup", target=task_id, detail=f"{existing['orig_name']} → {existing['id']}"
        )
        self._publish_task(task_id, extra={"file_id": existing["id"]})
        logger.info("task %s deduped into existing file %s", task_id, existing["id"])
        return await self.get_task(task_id)

    @staticmethod
    def _hash_timeout(total_size: int) -> float:
        return min(total_size / HASH_BYTES_PER_SECOND + HASH_BASE_TIMEOUT, HASH_MAX_TIMEOUT)

    # ------------------------------------------------------------------
    # 批量取件：目录展开 + 批量建任务
    # ------------------------------------------------------------------

    async def preview_selection(self, agent_id: str, paths: list[str]) -> dict[str, Any]:
        """把"一组路径（文件或目录，可混合）"展开成确定的文件清单。

        只做统计，不建任何任务——用户得先知道"这个目录里到底有多少东西"，
        再决定要不要取。这也是为什么目录勾选不能由前端算：懒加载的树不知道
        未展开的子目录里有什么，只有 B 自己遍历过才算"确定"。

        边界是硬的：超过 max_files / max_depth / timeout 就停下来，
        返回 `truncated=True`，让用户自己缩小范围，而不是在这里无限遍历。
        """
        if not self.hub.is_online(agent_id):
            raise ApiError(409, ErrorCode.AGENT_OFFLINE, "节点不在线，无法统计目录内容")

        files: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        seen: set[str] = set()
        truncated = False
        deadline = time.monotonic() + self.settings.batch_timeout_seconds
        dirs: list[tuple[str, int]] = []

        timeout = self.settings.command_timeout_seconds

        # ① 先看用户选的是文件还是目录。选中项通常只有几个，逐个 stat 很便宜。
        for raw in paths:
            try:
                rel = normalize_client_path(raw)
            except ValueError as exc:
                skipped.append({"rel": str(raw), "reason": str(exc)})
                continue
            if not rel:
                skipped.append({"rel": "/", "reason": "根目录不能整目录取件"})
                continue

            try:
                info = await self.hub.request(
                    agent_id, CommandName.STAT_FILE, {"rel": rel}, timeout=timeout
                )
            except AgentError as exc:
                skipped.append({"rel": rel, "reason": exc.message})
                continue

            if info.get("is_dir"):
                dirs.append((rel, 0))
            elif rel not in seen:
                seen.add(rel)
                files.append(
                    {
                        "rel": rel,
                        "name": str(info.get("name") or rel),
                        "size": int(info.get("size") or 0),
                    }
                )

        # ② 广度优先遍历目录。list_dir 自带 size（来自 scandir），不需要逐个 stat。
        while dirs and not truncated:
            if time.monotonic() > deadline:
                truncated = True
                skipped.append({"rel": "", "reason": f"统计超过 {self.settings.batch_timeout_seconds:g}s，已部分展开"})
                break

            rel, depth = dirs.pop(0)
            if depth >= self.settings.batch_max_depth:
                truncated = True
                skipped.append({"rel": rel, "reason": f"目录层级超过 {self.settings.batch_max_depth} 层"})
                break

            cursor: str | None = None
            while True:
                try:
                    page = await self.hub.request(
                        agent_id,
                        CommandName.LIST_DIR,
                        {"rel": rel, "cursor": cursor, "limit": 500},
                        timeout=timeout,
                    )
                except AgentError as exc:
                    skipped.append({"rel": rel, "reason": exc.message})
                    break

                for entry in page.get("entries") or []:
                    kind = entry.get("type")
                    entry_rel = str(entry.get("rel") or "")
                    if not entry_rel:
                        continue
                    if kind == "dir":
                        dirs.append((entry_rel, depth + 1))
                        continue
                    if kind != "file":
                        # 符号链接：list_dir 给的是链接自身的 size，不是目标的。
                        # 这里补一次 stat 拿真实大小——沙箱会校验目标是否在根目录内，
                        # 跟着走不出去；出不去或断链就记进 skipped。
                        try:
                            info = await self.hub.request(
                                agent_id,
                                CommandName.STAT_FILE,
                                {"rel": entry_rel},
                                timeout=timeout,
                            )
                        except AgentError as exc:
                            skipped.append({"rel": entry_rel, "reason": exc.message})
                            continue
                        if info.get("is_dir"):
                            # 指向目录的链接不展开：容易成环，且没有任何收益
                            skipped.append({"rel": entry_rel, "reason": "指向目录的符号链接不展开"})
                            continue
                        entry = info

                    if entry_rel in seen:
                        continue
                    seen.add(entry_rel)
                    files.append(
                        {
                            "rel": entry_rel,
                            "name": str(entry.get("name") or entry_rel),
                            "size": int(entry.get("size") or 0),
                        }
                    )
                    if len(files) > self.settings.batch_max_files:
                        truncated = True
                        break

                if truncated or not page.get("next_cursor"):
                    break
                cursor = str(page["next_cursor"])

        if len(files) > self.settings.batch_max_files:
            files = files[: self.settings.batch_max_files]
            truncated = True
            skipped.append(
                {"rel": "", "reason": f"超过单次 {self.settings.batch_max_files} 个文件的上限"}
            )

        return {
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files),
            "skipped": skipped[:50],
            "skipped_count": len(skipped),
            "truncated": truncated,
        }

    async def create_tasks_from_files(
        self, agent_id: str, files: list[dict[str, Any]], *, actor: str = "admin"
    ) -> dict[str, Any]:
        """按清单批量建任务。

        立刻返回，真正的建任务在后台跑：每个文件都要 stat → 磁盘预检 →
        预分配 .part → start_upload，几百个塞进一个 HTTP 请求必然超时。
        """
        if not files:
            raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, "没有可提交的文件")
        if not self.hub.is_online(agent_id):
            raise ApiError(409, ErrorCode.AGENT_OFFLINE, "节点不在线，无法下发取件任务")

        total_bytes = sum(int(item.get("size") or 0) for item in files)
        # 整批预检：单项各自检查的话，每一项都过得去、加起来却放不下，
        # 就会传一半才炸。
        try:
            self.storage.ensure_capacity(total_bytes)
        except StorageError as exc:
            raise ApiError(507, exc.code, exc.message) from exc

        job = asyncio.create_task(self._run_batch(agent_id, files, actor))
        self._batch_jobs.add(job)
        job.add_done_callback(self._batch_jobs.discard)
        return {"accepted": len(files), "total_bytes": total_bytes}

    async def _run_batch(
        self, agent_id: str, files: list[dict[str, Any]], actor: str
    ) -> None:
        semaphore = asyncio.Semaphore(self.settings.batch_create_concurrency)
        failed = 0

        async def one(item: dict[str, Any]) -> None:
            nonlocal failed
            async with semaphore:
                try:
                    await self.create_task(agent_id, str(item["rel"]), actor=actor)
                except ApiError as exc:
                    failed += 1
                    logger.info("batch item %s skipped: %s", item.get("rel"), exc.message)
                except Exception:  # 单项失败不能拖垮整批
                    failed += 1
                    logger.exception("batch item %s failed", item.get("rel"))

        await asyncio.gather(*(one(item) for item in files))

        await self.auth.audit(
            actor, "task.batch", target=agent_id, detail=f"files={len(files)} failed={failed}"
        )
        self.bus.publish(
            "batch_done", {"agent_id": agent_id, "total": len(files), "failed": failed}
        )
        logger.info("batch for %s done: %d file(s), %d failed", agent_id, len(files), failed)

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
    # 节点移除
    # ------------------------------------------------------------------

    async def remove_agent(
        self, agent_id: str, *, delete_files: bool = False, actor: str = "admin"
    ) -> dict[str, Any]:
        """注销一台 B。顺序是「先吊销 → 再断开 → 收尾任务 → 最后删行」。

        - **先吊销**：`revoked_at` 一写，token 立刻失效，B 的自助轮换也走不通
          （轮换要求旧 token 仍然有效）。这一步同时是安全网：后面任何一步失败，
          节点也已经不可用了，管理员修好再删即可。
        - **再断开**：token 只在握手时校验一次，不主动断开的话，那台机器在
          DELETE 返回之后仍能凭既有连接继续下传。
        - **收尾任务**：task 停在 uploading、staging 却没删，就是一块永久占盘的
          孤儿文件——`sweep_idle_uploads` 只清超时的，节点没了它永远不超时。
        - **文件默认保留**：A 的磁盘是这些文件的唯一副本，删节点不等于删数据。
          要清就走 `delete_files`，那是真删——所以默认不删。
        """
        row = await self.db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, f"没有这个节点：{agent_id}")

        # ① 先吊销，且必须早于断开：否则 B 会在"断开→删行"的窗口里换到新 token。
        await self.auth.revoke_agent(agent_id)

        # ② 再断开，否则既有连接还能继续下传。
        conn = self.hub.get(agent_id)
        if conn is not None:
            await conn.shutdown("node removed by admin", code=4401)
            await self.hub.unregister(conn)

        now = time.time()
        running = await self.db.fetchall(
            "SELECT id FROM tasks WHERE agent_id = ? AND status NOT IN (?, ?, ?)",
            (agent_id, DONE, FAILED, CANCELED),
        )
        for task in running:
            await self.db.execute(
                "UPDATE tasks SET status = ?, error_code = ?, error = ?, updated_at = ? WHERE id = ?",
                (CANCELED, ErrorCode.AGENT_OFFLINE.value, "节点已被删除", now, task["id"]),
            )

        pending = await self.db.fetchall(
            "SELECT id FROM uploads WHERE agent_id = ? AND status NOT IN (?, ?, ?)",
            (agent_id, DONE, FAILED, CANCELED),
        )
        for upload in pending:
            await self.db.execute(
                "UPDATE uploads SET status = ?, updated_at = ? WHERE id = ?",
                (CANCELED, now, upload["id"]),
            )
            try:
                await self.storage.drop_staging(str(upload["id"]))
            except StorageError:
                logger.warning("staging for upload %s was not removed", upload["id"])

        files = await self.db.fetchall(
            "SELECT id, stored_path FROM files WHERE agent_id = ?", (agent_id,)
        )
        removed = 0
        if delete_files:
            for file_row in files:
                try:
                    await self.storage.delete_blob(file_row["stored_path"])
                except OSError:
                    logger.warning("blob for file %s was not removed", file_row["id"])
                await self.db.execute("DELETE FROM files WHERE id = ?", (file_row["id"],))
                removed += 1

        await self.db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        await self.auth.audit(
            actor,
            "agent.delete",
            target=agent_id,
            detail=f"{row['name']} tasks={len(running)} files={len(files)} removed={removed}",
        )
        self.bus.publish("agent_removed", {"agent_id": agent_id, "name": row["name"]})
        logger.info(
            "agent %s (%s) removed: %d task(s) canceled, %d file(s) kept, %d removed",
            agent_id,
            row["name"],
            len(running),
            len(files) - removed,
            removed,
        )
        return {
            "ok": True,
            "agent_id": agent_id,
            "name": str(row["name"]),
            "tasks_canceled": len(running),
            "files_kept": len(files) - removed,
            "files_removed": removed,
        }

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
        # deduped 也走这个分支：收尾时 uploads 和 tasks 在同一个事务里落成了
        # done/deduped，B 重发 complete 不该拿到 409——那会让它在本地报失败，
        # 把一条已经正常收尾的任务改成 failed。
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
            # 只校验不落盘：先拿到真实哈希，才能决定这块数据要不要留。
            actual = await self.storage.verify(upload_id, expected_sha256=sha256_reported)
        except StorageError as exc:
            # 校验失败保留 staging 供排查；其余错误清理掉
            keep = exc.code is ErrorCode.CHECKSUM_MISMATCH
            await self._fail_upload(upload_id, exc.code, exc.message, keep_staging=keep)
            raise ApiError(409 if keep else 400, exc.code, exc.message) from exc
        except Exception as exc:
            # 除了 StorageError 还会抛 OSError：读 staging 时盘掉了、ENOSPC 之类。
            # 这些都不是"文件传坏了"，但后果一样——不在这里收尾的话，upload 会
            # 永久停在 verifying，而 sweep 只扫 uploading，于是任务页永远转圈、
            # .part 永远占盘。CancelledError 继承 BaseException，不会被这里吞掉。
            logger.exception("verify for upload %s blew up", upload_id)
            await self._fail_upload(upload_id, ErrorCode.INTERNAL, f"落盘失败：{exc}")
            raise ApiError(500, ErrorCode.INTERNAL, f"落盘失败：{exc}") from exc

        # 落盘前再判一次重。上传前那次可能没命中：老版本 agent 不支持 hash_file，
        # 或者两个相同内容在并发上传（谁都没落库，彼此都看不见）。
        rel = row["rel_path"] or row["filename"]
        size = int(row["total_size"])

        same = await self._file_at(row["agent_id"], rel)
        if same is not None and same["sha256"] == actual:
            # 同一路径、内容没变：这份字节是多余的，丢掉。
            await self.storage.drop_staging(upload_id)
            return await self._mark_task_deduped(
                row["task_id"], same, upload_id=upload_id, transferred=size, actor="agent"
            )

        twin = await self._file_with_sha(row["agent_id"], actual)
        if twin is not None:
            # 内容在别的路径上已经有了一份：不落新盘，硬链接过去再建记录。
            return await self._store_linked(
                row["task_id"],
                agent_id=row["agent_id"],
                rel=rel,
                filename=row["filename"],
                size=size,
                source=twin,
                upload_id=upload_id,
                transferred=size,
                actor="agent",
            )

        try:
            if same is not None:
                await self.storage.commit_over(upload_id, same["stored_path"])
            else:
                await self.storage.commit(upload_id, row["agent_id"], file_id)
        except Exception as exc:
            logger.exception("commit for upload %s blew up", upload_id)
            await self._fail_upload(upload_id, ErrorCode.INTERNAL, f"落盘失败：{exc}")
            raise ApiError(500, ErrorCode.INTERNAL, f"落盘失败：{exc}") from exc

        now = time.time()
        if same is not None:
            # 同一路径、内容变了：原地换掉字节，行 id 和存储路径都不动。
            file_id = same["id"]
            stored_path = same["stored_path"]
        else:
            stored_path = str(
                self.storage.final_path(row["agent_id"], file_id).relative_to(self.settings.storage_dir)
            )

        async with self.db.write_transaction() as conn:
            if same is not None:
                await conn.execute(
                    "UPDATE files SET sha256 = ?, size = ?, created_at = ? WHERE id = ?",
                    (actual, size, now, file_id),
                )
            else:
                await conn.execute(
                    "INSERT INTO files (id, agent_id, orig_name, rel_path, size, sha256, stored_path,"
                    " created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (file_id, row["agent_id"], row["filename"], rel, size, actual, stored_path, now),
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
        """空闲超时的上传会话回收。B 断线后没人续传的，30 分钟就清掉。

        连 `verifying` 一起扫是兜底而非正常路径：进入这个状态后只有一个 await
        在跑（重算 sha256），正常情况下几十秒内就走到 done/failed。它之所以需要
        兜底，是因为除了 `complete_upload` 自己 catch 的错误，还有进程被 kill、
        连接被掐断这类外部中断——那些情况下没有任何人会把状态推走。

        用 30 分钟的 `updated_at` 做门槛就不会误杀正在校验的上传：即便 20GB 在
        慢盘上要跑两分多钟，离 30 分钟也差得远。
        """
        cutoff = time.time() - self.settings.upload_idle_minutes * 60
        rows = await self.db.fetchall(
            "SELECT id, status FROM uploads WHERE status IN (?, ?) AND updated_at < ?",
            (UPLOADING, VERIFYING, cutoff),
        )
        for row in rows:
            message = (
                "校验阶段长时间没有结果，已回收"
                if row["status"] == VERIFYING
                else "上传会话空闲超时，已回收"
            )
            await self._fail_upload(row["id"], ErrorCode.TIMEOUT, message)
        return len(rows)

    # ------------------------------------------------------------------
    # 文件库
    # ------------------------------------------------------------------

    async def list_files(
        self,
        *,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("orig_name LIKE ?")
            params.append(f"%{query}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.db.fetchall(
            f"SELECT * FROM files {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        total = await self.db.fetchval(f"SELECT COUNT(*) FROM files {where}", params, default=0)
        return {
            "items": [self._file_view(r) for r in rows],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }

    # ------------------------------------------------------------------
    # 按目录浏览（文件库的树）
    #
    # 目录不是一个实体，它就是 rel_path 的前缀。所以这里不需要新表：一次查询
    # 按"下一段路径"分组，就得到这一层的子目录。子目录的 files/size 是**递归**
    # 统计的，展开前就知道这个文件夹里有多少东西、多大。
    # ------------------------------------------------------------------

    async def browse(
        self,
        *,
        agent_id: str,
        path: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        prefix = _clean_prefix(path)
        start = len(prefix) + 2 if prefix else 1
        like = _prefix_like(prefix)
        p = "COALESCE(rel_path, orig_name)"

        totals = await self.db.fetchone(
            f"SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS size FROM files"
            f" WHERE agent_id = ? AND (? = '' OR {p} LIKE ? ESCAPE '\\')",
            (agent_id, prefix, like),
        )

        dirs = await self.db.fetchall(
            f"SELECT substr(rest, 1, instr(rest, '/') - 1) AS name,"
            f" COUNT(*) AS files, COALESCE(SUM(size), 0) AS size"
            f" FROM (SELECT substr({p}, ?) AS rest, size FROM files"
            f"       WHERE agent_id = ? AND (? = '' OR {p} LIKE ? ESCAPE '\\'))"
            f" WHERE instr(rest, '/') > 0 GROUP BY name ORDER BY name",
            (start, agent_id, prefix, like),
        )

        rows = await self.db.fetchall(
            f"SELECT id, orig_name, {p} AS path, size, created_at FROM files"
            f" WHERE agent_id = ? AND (? = '' OR {p} LIKE ? ESCAPE '\\')"
            f" AND instr(substr({p}, ?), '/') = 0"
            f" ORDER BY orig_name LIMIT ? OFFSET ?",
            (agent_id, prefix, like, start, limit, offset),
        )

        return {
            "agent_id": agent_id,
            "path": prefix,
            "dirs": [
                {
                    "name": row["name"],
                    "path": _join_path(prefix, row["name"]),
                    "files": int(row["files"]),
                    "size": int(row["size"]),
                }
                for row in dirs
            ],
            "files": [
                {
                    "id": row["id"],
                    "name": row["orig_name"],
                    "path": row["path"],
                    "size": int(row["size"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "total_files": int(totals["files"]),
            "total_size": int(totals["size"]),
            "limit": limit,
            "offset": offset,
        }

    async def archive(self, *, agent_id: str, path: str) -> tuple[Path, str]:
        """把一个目录打成 zip。返回 (临时文件, 下载名)。"""
        prefix = _clean_prefix(path)
        like = _prefix_like(prefix)
        p = "COALESCE(rel_path, orig_name)"

        rows = await self.db.fetchall(
            f"SELECT id, orig_name, {p} AS path, stored_path FROM files"
            f" WHERE agent_id = ? AND (? = '' OR {p} LIKE ? ESCAPE '\\')"
            f" ORDER BY {p}",
            (agent_id, prefix, like),
        )
        if not rows:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "这个目录里没有可打包的文件")

        label = basename_of(prefix) if prefix else "files"
        target = self.settings.storage_dir / "tmp" / f"{uuid.uuid4().hex}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as bundle:
                for row in rows:
                    blob = self.settings.storage_dir / row["stored_path"]
                    if not blob.is_file():
                        logger.warning("archive skipped missing blob %s", blob)
                        continue
                    if prefix:
                        member = row["path"][len(prefix) + 1 :]
                        bundle.write(blob, arcname=f"{label}/{member}")
                    else:
                        bundle.write(blob, arcname=row["path"])

        # 打包是密集的同步 IO：直接写在协程里会卡住事件循环，心跳发不出去。
        await asyncio.to_thread(_write)
        return target, f"{label}.zip"

    async def delete_tree(self, *, agent_id: str, path: str, actor: str = "admin") -> dict[str, int]:
        """删掉一个目录下的所有文件。真删，和单文件删除同一套语义。"""
        prefix = _clean_prefix(path)
        if not prefix:
            raise ApiError(400, ErrorCode.INVALID_ARGUMENTS, "整库删除请用节点管理，不要从这里下手")

        like = _prefix_like(prefix)
        p = "COALESCE(rel_path, orig_name)"
        rows = await self.db.fetchall(
            f"SELECT id, stored_path FROM files WHERE agent_id = ?"
            f" AND {p} LIKE ? ESCAPE '\\'",
            (agent_id, like),
        )
        if not rows:
            return {"deleted": 0}

        ids = [str(row["id"]) for row in rows]
        for row in rows:
            await self.storage.delete_blob(row["stored_path"])

        for chunk in _chunks(ids, 400):
            placeholders = ",".join("?" * len(chunk))
            await self.db.execute(f"DELETE FROM files WHERE id IN ({placeholders})", chunk)

        await self.auth.audit(
            actor, "file.delete_tree", target=agent_id, detail=f"{prefix} · {len(ids)} 个文件"
        )
        self.bus.publish("files_changed", {"agent_id": agent_id, "path": prefix, "removed": len(ids)})
        logger.info("deleted %d file(s) under %s:%s", len(ids), agent_id, prefix)
        return {"deleted": len(ids)}

    async def sweep_archive_temp(self, *, max_age: float = 3600.0) -> int:
        """清理打包下载留下的临时 zip。

        正常路径是响应结束时由 BackgroundTask 删掉；只有进程被打死才会剩下。
        一小时的门槛足够宽：一个正在下载的包，它的 mtime 不会变，但也不会
        慢到一个小时还没下完。
        """
        folder = self.settings.storage_dir / "tmp"
        cutoff = time.time() - max_age

        def _scan() -> int:
            if not folder.exists():
                return 0
            removed = 0
            for entry in folder.glob("*.zip"):
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
            return removed

        return await asyncio.to_thread(_scan)

    async def soft_delete(self, file_id: str, *, actor: str = "admin") -> None:
        """删除一个文件。**真删**：行和 blob 一起消失，没有第二次机会。

        想反悔只有前端那 8 秒的撤销窗口——它靠的是"还没发请求"，不是服务端
        先存着。所以这里不需要 deleted_at，也不需要 trash 目录。
        """
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "文件不存在")
        await self.storage.delete_blob(row["stored_path"])
        await self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        await self.auth.audit(actor, "file.delete", target=file_id, detail=row["orig_name"])
        self.bus.publish("file_removed", {"file_id": file_id})

    async def purge_legacy_trash(self) -> int:
        """清空历史回收站。一次性：升级到"删除即真删"之后用不上了。

        这些文件本来就是用户主动删的，B 上还有原件，所以直接清掉而不是还原——
        还原等于把用户已经做出的决定撤销掉，还得再删一次。
        """
        rows = await self.db.fetchall("SELECT id FROM files WHERE deleted_at IS NOT NULL")
        for row in rows:
            await self.storage.purge(str(row["id"]))
        if rows:
            await self.db.execute("DELETE FROM files WHERE deleted_at IS NOT NULL")
            logger.info("purged %d file(s) left in the legacy trash", len(rows))
        return len(rows)

    async def storage_path_for(self, file_id: str) -> tuple[Any, Any]:
        row = await self.db.fetchone("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise ApiError(404, ErrorCode.FILE_NOT_FOUND, "文件不存在")
        return row, self.settings.storage_dir / str(row["stored_path"])

    # ------------------------------------------------------------------
    # 视图与小工具
    # ------------------------------------------------------------------

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
        }

    def _publish_task(self, task_id: str, extra: dict[str, Any] | None = None) -> None:
        self.bus.publish("task_update", {"task_id": task_id, **(extra or {})})

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
