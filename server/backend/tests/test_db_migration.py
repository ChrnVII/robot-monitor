"""老库升级：新增的列必须自己长出来。

`CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做——所以给 `agents` 加
`capabilities` 之后，线上那份库不会自动多出这两列，第一次写 hello 就会
`no such column`。这段逻辑没法靠"新库测试"覆盖，只能拿一份老结构单独验。
"""

from __future__ import annotations

import aiosqlite

from robot_monitor_server.db import Database

#: 加 capabilities 之前的 agents 表结构（照抄当时的 SCHEMA）。
OLD_AGENTS = """
CREATE TABLE agents (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    token_hash     TEXT NOT NULL,
    hostname       TEXT,
    os             TEXT,
    agent_version  TEXT,
    roots          TEXT NOT NULL DEFAULT '[]',
    last_seen      REAL,
    online         INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    revoked_at     REAL
);
"""


async def make_old_database(path) -> None:
    conn = await aiosqlite.connect(path)
    try:
        await conn.executescript(OLD_AGENTS)
        await conn.execute(
            "INSERT INTO agents (id, name, token_hash, roots, created_at)"
            " VALUES ('b-old', 'old-node', 'x', '[\"/srv/share\"]', 0)"
        )
        await conn.commit()
    finally:
        await conn.close()


async def test_existing_rows_get_the_new_columns(tmp_path):
    path = tmp_path / "robot-monitor.db"
    await make_old_database(path)

    db = Database(path)
    await db.connect()
    try:
        row = await db.fetchone(
            "SELECT capabilities, media_profiles FROM agents WHERE id = 'b-old'"
        )
        # 老节点从没声明过能力：默认空数组，列表接口才不会 500。
        assert row["capabilities"] == "[]"
        assert row["media_profiles"] == "[]"
    finally:
        await db.close()


async def test_upgrade_is_idempotent(tmp_path):
    """每次启动都会跑一遍补列逻辑，重复执行必须无害。"""
    path = tmp_path / "robot-monitor.db"
    await make_old_database(path)

    for _ in range(3):
        db = Database(path)
        await db.connect()
        await db.close()
