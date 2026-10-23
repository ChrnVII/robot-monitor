"""SQLite 访问层。

三个刻意的选择：

1. **单连接 + 写锁。** SQLite 的并发写在多连接下会 `database is locked`，
   而本系统只有一台 A、单 worker，连接池带来的并发收益远小于它带来的
   锁竞争。一个连接，写操作串行，读走 WAL 快照。

2. **WAL + synchronous=NORMAL。** 默认的 `DELETE` journal 在每次提交时都
   fsync，上传小块时这点开销会被放大成吞吐瓶颈。WAL 下提交只写日志尾部，
   崩溃最坏情况是丢最后一次提交，对"文件库里少一条记录"是可接受的，
   因为文件本体还在 staging，重传即可。

3. **时间统一存 REAL（unix 秒）。** 不用 SQLite 的日期字符串：跨时区、
   跨 SQLite 版本的字符串解析都不可靠，而且 SSE 里也要用数字时间戳。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);

-- 单行表：管理员凭据。放在库里而不是 env，是为了让密码可以随时改而不用重启。
-- env 里的 hash 仍然被支持，优先级更低（见 auth._credentials）。
CREATE TABLE IF NOT EXISTS admin_user (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    updated_at    REAL NOT NULL
);

-- 待批准的设备注册请求。不存明文 token：批准后由 B 来领取时现场签发，
-- 领一次即作废（status=claimed）。
CREATE TABLE IF NOT EXISTS enroll_requests (
    id          TEXT PRIMARY KEY,
    hostname    TEXT,
    os          TEXT,
    root_path   TEXT,
    remote_ip   TEXT,
    status      TEXT NOT NULL,          -- pending | approved | rejected | claimed
    agent_id    TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enroll_status ON enroll_requests(status, created_at);

-- 单用户部署只建一个管理员，密码哈希放配置；agents 表虽然只有一行也保留 id，
-- 因为换机器时 token 要能独立吊销重建，不能把身份写死在代码里。
CREATE TABLE IF NOT EXISTS agents (
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

CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    agent_id           TEXT NOT NULL,
    type               TEXT NOT NULL DEFAULT 'fetch',
    src_path           TEXT NOT NULL,
    status             TEXT NOT NULL,
    file_id            TEXT,
    upload_id          TEXT,
    total_bytes        INTEGER,
    transferred_bytes  INTEGER NOT NULL DEFAULT 0,
    error_code         TEXT,
    error              TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS uploads (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    filename      TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    total_size    INTEGER NOT NULL,
    chunk_size    INTEGER NOT NULL,
    total_chunks  INTEGER NOT NULL,
    received_json TEXT NOT NULL DEFAULT '[]',
    sha256_expected TEXT,
    status        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_status ON uploads(status, updated_at);

CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    orig_name   TEXT NOT NULL,
    rel_path    TEXT,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    created_at  REAL NOT NULL,
    deleted_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_files_created ON files(deleted_at, created_at DESC);

-- 审计日志：每一次文件操作都要能还原出"谁、什么时候、对哪个文件做了什么"
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
"""


class Database:
    """一个连接，一把写锁。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected; call connect() first")
        return self._conn

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- 写 -------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._write_lock:
            await self.conn.execute(sql, params)
            await self.conn.commit()

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """持锁执行多条语句，失败回滚。

        块上传要"读 received_json → 合并 → 写回"，两个块并发到达时如果不在
        一个事务里，后写的会把先写的块号覆盖掉——表现是进度偶尔倒退、
        complete 时报缺块，而且极难复现。
        """
        async with self._write_lock:
            try:
                yield self.conn
            except Exception:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()

    # --- 读 -------------------------------------------------------------

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        try:
            return await cur.fetchone()
        finally:
            await cur.close()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        try:
            return list(await cur.fetchall())
        finally:
            await cur.close()

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return default
        return row[0]
