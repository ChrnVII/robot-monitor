"""重复内容的清理命令。

判重上线后不会再产生新的重复，但历史数据里可能已经有。这个命令的职责是
"合并存储"而不是"删数据"：每一行 files 都还在，只是多个路径指向同一份 inode。
所以两条断言最要紧——链接之后**数据仍然可读**，以及**不留下临时文件**。
"""

from __future__ import annotations

import time

from filebridge_server.cli import _duplicate_groups, _link_over
from filebridge_server.config import Settings
from filebridge_server.db import Database


def test_link_over_points_both_paths_at_one_inode(tmp_path):
    base = tmp_path / "storage"
    (base / "aa").mkdir(parents=True)
    (base / "bb").mkdir(parents=True)
    (base / "aa" / "one").write_bytes(b"same bytes")
    (base / "bb" / "two").write_bytes(b"same bytes")

    assert _link_over(base, "aa/one", "bb/two") is True

    first = base / "aa" / "one"
    second = base / "bb" / "two"
    assert first.stat().st_ino == second.stat().st_ino
    assert first.read_bytes() == b"same bytes"
    assert second.read_bytes() == b"same bytes"
    # 临时名不能留在库里，否则下次统计会把它们当文件
    assert not list((base / "bb").glob("*.dedup"))


def test_link_over_refuses_missing_file(tmp_path):
    base = tmp_path / "storage"
    base.mkdir()
    (base / "only").write_bytes(b"x")

    assert _link_over(base, "only", "gone") is False


async def test_duplicate_groups_ignores_trash_and_orders_by_wasted_space(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", storage_dir=tmp_path / "storage")
    settings.ensure_dirs()

    db = Database(settings.db_path)
    await db.connect()
    try:
        now = time.time()
        rows = [
            # 两份 400 字节的重复 + 一份 4 字节的重复 + 一条孤立的
            ("f1", "a" * 64, 400, None),
            ("f2", "a" * 64, 400, None),
            ("f3", "b" * 64, 4, None),
            ("f4", "b" * 64, 4, None),
            # 进了回收站的那条不该参与“可释放空间”的计算
            ("f5", "c" * 64, 999, now),
            ("f6", "c" * 64, 999, None),
            ("f7", "d" * 64, 7, None),
        ]
        for file_id, digest, size, deleted_at in rows:
            await db.execute(
                "INSERT INTO files (id, agent_id, orig_name, rel_path, size, sha256,"
                " stored_path, created_at, deleted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (file_id, "ag", f"{file_id}.bin", f"{file_id}.bin", size, digest, file_id, now, deleted_at),
            )

        groups = await _duplicate_groups(db)

        assert [(group["sha256"], group["copies"]) for group in groups] == [
            ("a" * 64, 2),
            ("b" * 64, 2),
        ]
    finally:
        await db.close()
