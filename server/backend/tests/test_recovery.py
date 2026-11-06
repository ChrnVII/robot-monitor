"""上传收尾的失败路径。

测两条性质：

1. **任何一步炸了都不能留下僵尸。** `complete` 阶段除了 `StorageError` 还会
   抛 `OSError`（mkdir 没权限、`os.replace` 撞 ENOSPC、读 staging 时盘掉了）。
   如果只 catch 前者，upload 会永久停在 `verifying`、task 停在 `uploading`，
   任务页永远转圈，`.part` 永远占盘。
2. **空闲清扫能捞到卡住的 `verifying`，但不会误杀正在校验的那一个。**
   正在算 sha256 的上传是活着的，只是慢；用 30 分钟的 `updated_at` 做门槛
   就能把两者分开。
"""

from __future__ import annotations

import time

import pytest

from filebridge_server.auth import AuthService
from filebridge_server.config import Settings
from filebridge_server.db import Database
from filebridge_server.errors import ApiError
from filebridge_server.events import EventBus
from filebridge_server.protocol import CommandName
from filebridge_server.services import TaskService
from filebridge_server.storage import Storage

# 两块：300KB 的文件配 256KB 的块
SIZE = 300 * 1024
CHUNK = 256 * 1024


class FakeHub:
    """只回答 stat_file / start_upload，够让 create_task 走完整条路。"""

    def __init__(self) -> None:
        self.commands: list[CommandName] = []

    def is_online(self, agent_id: str) -> bool:
        return True

    async def request(self, agent_id, command, payload=None, *, timeout=None):
        self.commands.append(command)
        if command is CommandName.STAT_FILE:
            return {
                "rel": (payload or {}).get("rel", ""),
                "name": "payload.bin",
                "is_dir": False,
                "size": SIZE,
                "mtime": 0.0,
            }
        return {"accepted": True}


@pytest.fixture()
async def stack(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        chunk_size_small=CHUNK,
        chunk_size_large=CHUNK,
    )
    settings.ensure_dirs()
    db = Database(settings.db_path)
    await db.connect()
    service = TaskService(
        db, settings, FakeHub(), Storage(settings), EventBus(), AuthService(db, settings)
    )
    try:
        yield settings, db, service
    finally:
        await db.close()


async def started_upload(service: TaskService) -> tuple[str, str]:
    task = await service.create_task("agent-1", "payload.bin")
    return task["id"], task["upload_id"]


async def feed_all_chunks(service: TaskService, upload_id: str) -> None:
    upload = await service.get_upload(upload_id)
    total = int(upload["total_size"])
    chunk = int(upload["chunk_size"])
    for index in range(int(upload["total_chunks"])):
        offset = index * chunk
        await service.add_chunk(upload_id, index, b"\0" * min(chunk, total - offset))


async def status_of(db: Database, *, task_id: str, upload_id: str) -> tuple[str, str]:
    upload = await db.fetchone("SELECT status FROM uploads WHERE id = ?", (upload_id,))
    task = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
    return upload["status"], task["status"]


async def test_finalize_crash_lands_in_terminal_state(stack, monkeypatch):
    """`finalize` 抛 OSError 时，upload 和 task 必须一起落到 failed。"""
    settings, db, service = stack
    task_id, upload_id = await started_upload(service)
    await feed_all_chunks(service, upload_id)

    async def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service.storage, "finalize", boom)

    with pytest.raises(ApiError) as excinfo:
        await service.complete_upload(upload_id, None)
    assert excinfo.value.status_code == 500

    upload_status, task_status = await status_of(db, task_id=task_id, upload_id=upload_id)
    assert upload_status == "failed"
    assert task_status == "failed"
    # staging 要一起清掉：库里已经是终态，没有人会再来认领这个 .part
    assert list(settings.staging_dir.glob("*.part")) == []


async def test_idle_sweep_reclaims_stuck_verifying(stack):
    """进程在校验阶段被打断 → 只剩空闲清扫能收尸。"""
    _settings, db, service = stack
    task_id, upload_id = await started_upload(service)
    await feed_all_chunks(service, upload_id)

    # updated_at=0：模拟"卡了很久，早就该超时"
    await db.execute(
        "UPDATE uploads SET status = 'verifying', updated_at = 0 WHERE id = ?", (upload_id,)
    )

    assert await service.sweep_idle_uploads() == 1

    upload_status, task_status = await status_of(db, task_id=task_id, upload_id=upload_id)
    assert upload_status == "failed"
    assert task_status == "failed"


async def test_idle_sweep_leaves_fresh_verifying_alone(stack):
    """正在算 sha256 的那一个不能被扫走——它是活着的，只是慢。

    这条是上一条改动的安全边界：20GB 在慢盘上要跑两分多钟，离 30 分钟
    的 idle 门槛差得远，所以放宽清扫范围不会误伤正常上传。
    """
    _settings, db, service = stack
    task_id, upload_id = await started_upload(service)
    await feed_all_chunks(service, upload_id)

    await db.execute(
        "UPDATE uploads SET status = 'verifying', updated_at = ? WHERE id = ?",
        (time.time(), upload_id),
    )

    assert await service.sweep_idle_uploads() == 0

    upload_status, task_status = await status_of(db, task_id=task_id, upload_id=upload_id)
    assert upload_status == "verifying"
    assert task_status == "uploading"


async def test_idle_sweep_still_covers_uploading(stack):
    """原有行为不能被改坏：传一半就没人管的 upload 仍然 30 分钟回收。"""
    _settings, db, service = stack
    task_id, upload_id = await started_upload(service)

    await db.execute("UPDATE uploads SET updated_at = 0 WHERE id = ?", (upload_id,))

    assert await service.sweep_idle_uploads() == 1

    upload_status, task_status = await status_of(db, task_id=task_id, upload_id=upload_id)
    assert upload_status == "failed"
    assert task_status == "failed"
