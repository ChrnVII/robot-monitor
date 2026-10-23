"""落盘：预分配 → 偏移写块 → 全量校验 → 原子改名。

三个关键决定：

1. **预分配单个 `.part` 文件，而不是每块一个文件。**
   几 GB 的文件如果每块存一个文件，complete 时要先拼装，磁盘峰值到文件大小
   的 2 倍（5GB 需要 10GB）。预分配 + 偏移写入的峰值只要 1 倍。代价是 complete
   时要顺序读一遍算 sha256：5GB 在 SSD 上约十秒，可以接受。

2. **每次写块独立开句柄，不用常驻 fd。** 不同句柄各自持文件位置，天然互不干扰；
   1–8MB 配一次 open/close 的开销相对数据量可忽略。常驻 fd + `os.pwrite` 更快，
   但要为每个进行中的上传维护生命周期和泄漏处理，等压测说需要再换。

3. **落盘前必须重算 sha256。** 客户端报的哈希只作为交叉校验，不能作为依据：
   校验和是防"传坏了"，信客户端报的等于没校验。

所有文件 IO 都扔线程池。这些是同步系统调用，直接写在协程里会卡住事件循环，
心跳发不出去，A 会误判 B 离线并把上传打断。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import shutil
from pathlib import Path

from .config import Settings
from .protocol import ErrorCode

DIGEST_BLOCK = 1024 * 1024


class StorageError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # --- 路径 -----------------------------------------------------------

    def staging_path(self, upload_id: str) -> Path:
        # upload_id 由服务端生成，但仍做白名单：任何进路径拼接的字符串都该校验
        if not upload_id.isalnum():
            raise StorageError(ErrorCode.INTERNAL, f"invalid upload id {upload_id!r}")
        return self.settings.staging_dir / f"{upload_id}.part"

    def final_path(self, agent_id: str, file_id: str) -> Path:
        if not file_id.isalnum() or not agent_id.isalnum():
            raise StorageError(ErrorCode.INTERNAL, "invalid storage id")
        # 两字符分片目录，避免单目录十万文件
        return self.settings.files_dir / agent_id / file_id[:2] / file_id

    def trash_path(self, file_id: str) -> Path:
        if not file_id.isalnum():
            raise StorageError(ErrorCode.INTERNAL, "invalid file id")
        return self.settings.trash_dir / file_id

    # --- 容量 -----------------------------------------------------------

    def free_bytes(self) -> int:
        return shutil.disk_usage(str(self.settings.storage_dir)).free

    def ensure_capacity(self, total_bytes: int) -> None:
        need = int(total_bytes * self.settings.disk_reserve_ratio)
        free = self.free_bytes()
        if free < need:
            raise StorageError(
                ErrorCode.INSUFFICIENT_SPACE,
                f"需要约 {need} 字节，磁盘只剩 {free} 字节",
            )

    # --- staging --------------------------------------------------------

    async def create_staging(self, upload_id: str, total_size: int) -> Path:
        path = self.staging_path(upload_id)
        await asyncio.to_thread(self._create_staging, path, total_size)
        return path

    @staticmethod
    def _create_staging(path: Path, total_size: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            if total_size:
                # 稀疏分配：把大小先占住，后续每块 seek 到自己的偏移写
                handle.truncate(total_size)

    async def write_chunk(self, upload_id: str, offset: int, data: bytes, total_size: int) -> None:
        if offset < 0 or offset + len(data) > total_size:
            raise StorageError(
                ErrorCode.INVALID_ARGUMENTS,
                f"chunk range [{offset}, {offset + len(data)}) exceeds file size {total_size}",
            )
        path = self.staging_path(upload_id)
        try:
            await asyncio.to_thread(self._write_chunk, path, offset, data)
        except FileNotFoundError as exc:
            raise StorageError(ErrorCode.FILE_NOT_FOUND, "upload staging file is gone") from exc

    @staticmethod
    def _write_chunk(path: Path, offset: int, data: bytes) -> None:
        with open(path, "r+b") as handle:
            handle.seek(offset)
            handle.write(data)

    async def drop_staging(self, upload_id: str) -> None:
        path = self.staging_path(upload_id)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    # --- 校验与提交 -----------------------------------------------------

    async def digest(self, path: Path) -> str:
        return await asyncio.to_thread(self._digest, path)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while block := handle.read(DIGEST_BLOCK):
                digest.update(block)
        return digest.hexdigest()

    async def finalize(
        self,
        upload_id: str,
        agent_id: str,
        file_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        """校验并原子落盘，返回实际 sha256。

        校验失败时保留 staging（便于排查到底哪一段坏了），不落盘。
        """
        part = self.staging_path(upload_id)
        if not part.exists():
            raise StorageError(ErrorCode.FILE_NOT_FOUND, "upload staging file is gone")

        actual = await self.digest(part)

        if expected_sha256 and not hmac.compare_digest(actual, expected_sha256.lower()):
            raise StorageError(
                ErrorCode.CHECKSUM_MISMATCH,
                f"sha256 mismatch: server computed {actual}, agent reported {expected_sha256}",
            )

        destination = self.final_path(agent_id, file_id)
        await asyncio.to_thread(self._commit, part, destination)
        return actual

    @staticmethod
    def _commit(part: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # 先把数据刷到盘上再改名。否则掉电可能留下"库里有的文件是空的"。
        with open(part, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(part, destination)

    # --- 回收站 ---------------------------------------------------------

    async def move_to_trash(self, agent_id: str, file_id: str) -> Path:
        source = self.final_path(agent_id, file_id)
        target = self.trash_path(file_id)
        await asyncio.to_thread(self._move, source, target)
        return target

    def _move(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)

    async def restore_from_trash(self, agent_id: str, file_id: str) -> Path:
        source = self.trash_path(file_id)
        target = self.final_path(agent_id, file_id)
        await asyncio.to_thread(self._move, source, target)
        return target

    async def purge(self, file_id: str) -> None:
        path = self.trash_path(file_id)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    # --- 开机清理 -------------------------------------------------------

    async def sweep_orphan_staging(self, keep: set[str]) -> list[str]:
        """删掉 staging 里没有对应 upload 行的 `.part`。

        A 重启后可能留下孤儿文件（进程被打死时来不及清理）。不清会一直占磁盘。
        """
        removed: list[str] = []

        def _scan() -> list[str]:
            if not self.settings.staging_dir.exists():
                return []
            for entry in self.settings.staging_dir.iterdir():
                if entry.suffix != ".part" or not entry.is_file():
                    continue
                upload_id = entry.stem
                if upload_id in keep:
                    continue
                entry.unlink(missing_ok=True)
                removed.append(upload_id)
            return removed

        return await asyncio.to_thread(_scan)
