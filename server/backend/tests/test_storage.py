"""落盘测试：预分配、偏移写、校验、原子改名。

这里有一条容易忽略但很值钱的性质：**写入顺序不影响结果**。分片并发到达时
顺序是随机的，如果实现成"顺序追加"，乱序到达就会写出错位的内容。
"""

from __future__ import annotations

import hashlib
import os

import pytest

from filebridge_server.config import Settings
from filebridge_server.protocol import ErrorCode
from filebridge_server.storage import Storage, StorageError

PAYLOAD = bytes(range(256)) * 8192          # 2 MiB
CHUNK = 64 * 1024


@pytest.fixture()
def storage(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        chunk_size_small=CHUNK,
        chunk_size_large=CHUNK,
    )
    settings.ensure_dirs()
    return Storage(settings)


async def upload_all(storage: Storage, upload_id: str, data: bytes) -> None:
    await storage.create_staging(upload_id, len(data))
    for index, offset in enumerate(range(0, len(data), CHUNK)):
        await storage.write_chunk(upload_id, offset, data[offset : offset + CHUNK], len(data))


async def test_staging_is_preallocated(storage):
    path = await storage.create_staging("up1", 3 * 1024 * 1024)

    assert path.exists()
    assert path.stat().st_size == 3 * 1024 * 1024


async def test_chunks_written_out_of_order_still_correct(storage):

    total = len(PAYLOAD)
    await storage.create_staging("up2", total)

    offsets = list(range(0, total, CHUNK))
    # 打乱顺序写，模拟并发到达
    for offset in reversed(offsets):
        await storage.write_chunk("up2", offset, PAYLOAD[offset : offset + CHUNK], total)

    digest = await storage.digest(storage.staging_path("up2"))
    assert digest == hashlib.sha256(PAYLOAD).hexdigest()


async def test_chunk_out_of_range_is_rejected(storage):
    await storage.create_staging("up3", 100)

    with pytest.raises(StorageError) as excinfo:
        await storage.write_chunk("up3", 90, b"x" * 20, 100)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENTS

    with pytest.raises(StorageError):
        await storage.write_chunk("up3", -1, b"x", 100)


async def test_write_to_missing_staging_is_file_not_found(storage):
    with pytest.raises(StorageError) as excinfo:
        await storage.write_chunk("nope", 0, b"x", 1)
    assert excinfo.value.code is ErrorCode.FILE_NOT_FOUND


async def test_finalize_moves_file_and_removes_staging(storage):
    await upload_all(storage, "up4", PAYLOAD)
    expected = hashlib.sha256(PAYLOAD).hexdigest()

    actual = await storage.finalize("up4", "agent1", "file1", expected_sha256=expected)

    assert actual == expected
    final = storage.final_path("agent1", "file1")
    assert final.exists()
    assert final.read_bytes() == PAYLOAD
    assert not storage.staging_path("up4").exists()
    # 两字符分片目录
    assert final.parent.name == "fi"


async def test_checksum_mismatch_keeps_staging(storage):
    await upload_all(storage, "up5", PAYLOAD)

    with pytest.raises(StorageError) as excinfo:
        await storage.finalize("up5", "agent1", "file2", expected_sha256="0" * 64)

    assert excinfo.value.code is ErrorCode.CHECKSUM_MISMATCH
    # 保留 staging 供排查，不落盘
    assert storage.staging_path("up5").exists()
    assert not storage.final_path("agent1", "file2").exists()


async def test_server_digest_wins_over_client_claim(storage):
    """客户端报错哈希时，服务端算出来的才是权威值。"""
    await upload_all(storage, "up6", PAYLOAD)
    actual = await storage.digest(storage.staging_path("up6"))

    # 报一个"看起来对但不对"的哈希
    with pytest.raises(StorageError):
        await storage.finalize("up6", "agent1", "file3", expected_sha256=actual[:-1] + "0")


async def test_empty_file_hashes_to_empty_digest(storage):
    await storage.create_staging("up7", 0)
    actual = await storage.finalize("up7", "agent1", "file4")

    assert actual == hashlib.sha256(b"").hexdigest()
    assert storage.final_path("agent1", "file4").stat().st_size == 0


async def test_trash_roundtrip(storage):
    await upload_all(storage, "up8", PAYLOAD)
    await storage.finalize("up8", "agent1", "file5")

    trashed = await storage.move_to_trash("agent1", "file5")
    assert trashed.exists()
    assert not storage.final_path("agent1", "file5").exists()

    restored = await storage.restore_from_trash("agent1", "file5")
    assert restored.exists()

    await storage.move_to_trash("agent1", "file5")
    await storage.purge("file5")
    assert not trashed.exists()


async def test_orphan_staging_is_swept(storage):
    await storage.create_staging("keepme", 10)
    await storage.create_staging("orphan1", 10)

    removed = await storage.sweep_orphan_staging({"keepme"})

    assert removed == ["orphan1"]
    assert storage.staging_path("keepme").exists()
    assert not storage.staging_path("orphan1").exists()


def test_capacity_check_rejects_when_disk_is_too_small(storage):
    free = storage.free_bytes()
    storage.settings.disk_reserve_ratio = 1.2

    storage.ensure_capacity(int(free * 0.1))          # 应该通过
    with pytest.raises(StorageError) as excinfo:
        storage.ensure_capacity(free)                  # 需要 1.2 倍剩余空间
    assert excinfo.value.code is ErrorCode.INSUFFICIENT_SPACE


def test_invalid_ids_are_rejected(storage):
    with pytest.raises(StorageError):
        storage.staging_path("../etc/passwd")
    with pytest.raises(StorageError):
        storage.final_path("agent", "../../x")


def test_final_path_uses_two_char_shard(storage):
    path = storage.final_path("agent1", "abcdef123456")
    assert path.parts[-2] == "ab"
    assert os.path.basename(path) == "abcdef123456"
