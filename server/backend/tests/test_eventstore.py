"""事件入库、游标查询、状态落库。

三条被钉住的性质：

1. **重复不报错。** 补传窗口重叠是常态，云端必须幂等消费；
2. **`acked_seq` 只前进。** 回退会让机器人把已删除的事件再发一遍；
3. **重启后状态能装回来。** 但装回来的是旧 `observed_at`，页面要如实显示 stale。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from robot_monitor_server.auth import AuthService
from robot_monitor_server.config import Settings
from robot_monitor_server.db import Database
from robot_monitor_server.eventstore import EventStore
from robot_monitor_server.main import _restore_state, _save_state, create_app
from robot_monitor_server.state import MemoryStateStore

PASSWORD = "events-123"


@pytest.fixture()
async def db(tmp_path):
    database = Database(tmp_path / "monitor.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def event(seq: int, *, event_type: str = "alert.raised", **payload) -> dict:
    return {
        "event_seq": seq,
        "event_id": f"ev-{seq}",
        "event_type": event_type,
        "schema_version": 1,
        "occurred_at": 1761033600.0 + seq,
        "payload": payload or {"code": "person_intrusion"},
    }


# --- 入库 ---------------------------------------------------------------


async def test_ingest_returns_highest_sequence_as_ack(db):
    store = EventStore(db)

    result = await store.ingest("b-1", [event(1), event(2), event(3)])

    assert result["acked_seq"] == 3
    assert result["duplicates"] == 0
    assert result["received"] == 3
    assert (await store.stats("b-1"))["count"] == 3


async def test_duplicate_batch_is_idempotent(db):
    """同一批发两次：第二次全是重复，不报错、不多行。"""
    store = EventStore(db)
    batch = [event(1), event(2)]

    await store.ingest("b-1", batch)
    again = await store.ingest("b-1", batch)

    assert again["duplicates"] == 2
    assert (await store.stats("b-1"))["count"] == 2


async def test_ack_never_moves_backwards(db):
    """游标回退会让机器人重复发送已经删掉的事件。"""
    store = EventStore(db)
    await store.ingest("b-1", [event(10)])

    result = await store.ingest("b-1", [event(11)], cursor=10)

    assert result["acked_seq"] == 11


async def test_batch_without_sequence_is_skipped(db):
    store = EventStore(db)

    result = await store.ingest("b-1", [{"event_type": "x", "payload": {}}])

    assert result["acked_seq"] == 0
    assert (await store.stats("b-1"))["count"] == 0


async def test_events_are_isolated_per_agent(db):
    store = EventStore(db)
    await store.ingest("b-1", [event(1)])
    await store.ingest("b-2", [event(1)])

    assert len(await store.query(agent_id="b-1")) == 1
    assert len(await store.query(agent_id="b-2")) == 1


# --- 查询 ---------------------------------------------------------------


async def test_query_uses_cursor(db):
    store = EventStore(db)
    await store.ingest("b-1", [event(1), event(2), event(3)])

    rows = await store.query(agent_id="b-1", cursor=1)

    assert [row["event_seq"] for row in rows] == [2, 3]


async def test_query_filters_by_type(db):
    store = EventStore(db)
    await store.ingest("b-1", [event(1), event(2, event_type="event.gap")])

    rows = await store.query(agent_id="b-1", event_type="event.gap")

    assert [row["event_type"] for row in rows] == ["event.gap"]


# --- HTTP 接口 ----------------------------------------------------------


def bootstrap(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", storage_dir=tmp_path / "storage", serve_frontend=False
    )
    settings.ensure_dirs()

    async def run() -> tuple[str, str]:
        database = Database(settings.db_path)
        await database.connect()
        try:
            auth = AuthService(database, settings)
            await auth.set_password("admin", PASSWORD)
            return await auth.issue_agent_token("b-events", hostname="events")
        finally:
            await database.close()

    agent_id, token = asyncio.run(run())
    return settings, create_app(settings), agent_id, token


def test_internal_events_accepts_a_batch_and_dedups(tmp_path):
    _settings, app, agent_id, token = bootstrap(tmp_path)
    body = {"agent_id": agent_id, "cursor": 0, "batch": [event(1), event(2)]}

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        first = client.post("/internal/events", json=body, headers=headers)
        second = client.post("/internal/events", json=body, headers=headers)

        assert first.status_code == 200, first.text
        assert first.json()["acked_seq"] == 2
        assert second.json()["duplicates"] == 2

        client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        listing = client.get("/api/v1/events", params={"agent_id": agent_id})
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 2
        assert listing.json()["next_cursor"] == 2


def test_internal_events_requires_agent_token(tmp_path):
    _settings, app, _agent_id, _token = bootstrap(tmp_path)

    with TestClient(app) as client:
        response = client.post("/internal/events", json={"batch": [event(1)]})

    assert response.status_code == 401


# --- 状态落库与恢复 ------------------------------------------------------


async def test_state_survives_a_restart(tmp_path):
    """重启后装回来的状态要保留原来的 observed_at（否则会被当成实时数据）。"""
    database = Database(tmp_path / "monitor.db")
    await database.connect()
    try:
        store = MemoryStateStore()
        observed = time.time() - 40
        await store.put(
            "b-1",
            {"state_version": 7, "observed_at": observed, "robot": {"battery": 70}},
        )
        ctx = SimpleNamespace(db=database, states=store)

        await _save_state(ctx)
        assert await database.fetchval("SELECT COUNT(*) FROM agent_state") == 1

        fresh = MemoryStateStore()
        restored = SimpleNamespace(db=database, states=fresh)
        await _restore_state(restored)

        record = await fresh.get("b-1")
        assert record is not None
        assert record.state_version == 7
        assert record.observed_at == pytest.approx(observed)
        # 40 秒前的数据不新鲜：恢复不是"洗白"
        assert record.freshness() == "unknown"
    finally:
        await database.close()


async def test_save_state_upserts(tmp_path):
    database = Database(tmp_path / "monitor.db")
    await database.connect()
    try:
        store = MemoryStateStore()
        ctx = SimpleNamespace(db=database, states=store)
        await store.put("b-1", {"state_version": 1, "observed_at": time.time()})
        await _save_state(ctx)
        await store.put("b-1", {"state_version": 2, "observed_at": time.time()})
        await _save_state(ctx)

        rows = await database.fetchall("SELECT agent_id, state_version FROM agent_state")
        assert len(rows) == 1            # 每台设备只有一行
        assert rows[0]["state_version"] == 2
        saved = json.loads(await database.fetchval("SELECT state_json FROM agent_state"))
        assert saved["state_version"] == 2
    finally:
        await database.close()
