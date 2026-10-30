"""节点管理：删除节点、待批准接入的批准/拒绝。

这三件事以前只有 CLI 能做（`revoke-token` / `pending` / `approve`）。搬到网页端之后，
要钉住的不只是"接口能通"，还有两条语义：

1. **删节点不等于删数据。** 文件默认留着，勾选了也只是进回收站。
2. **批准动作本身不产生凭据。** token 仍然只在 B 领取时现场签发、且只签一次。
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from filebridge_server.auth import AuthService, hash_password
from filebridge_server.config import Settings
from filebridge_server.db import Database
from filebridge_server.main import create_app

PASSWORD = "agents-admin-123"


def make_app(tmp_path, **overrides):
    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        admin_password_hash=hash_password(PASSWORD),
        serve_frontend=False,
        **overrides,
    )
    return settings, create_app(settings)


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def enroll(client: TestClient, hostname: str = "lab-01", **payload) -> dict:
    response = client.post("/internal/enroll", json={"hostname": hostname, **payload})
    assert response.status_code in (200, 202), response.text
    return response.json()


async def _with_db(settings: Settings, action):
    db = Database(settings.db_path)
    await db.connect()
    try:
        return await action(db, AuthService(db, settings))
    finally:
        await db.close()


def authenticate(settings: Settings, token: str) -> bool:
    return asyncio.run(_with_db(settings, lambda _db, auth: auth.authenticate_agent(token))) is not None


def insert_file(settings: Settings, agent_id: str, *, with_disk_file: bool = False) -> str:
    """直接写一行 files。走真实上传太慢，这些用例要测的是删除语义。"""
    file_id = uuid.uuid4().hex
    path = settings.files_dir / agent_id / file_id[:2] / file_id
    if with_disk_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")

    async def action(db: Database, _auth: AuthService) -> None:
        await db.execute(
            "INSERT INTO files (id, agent_id, orig_name, rel_path, size, sha256,"
            " stored_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, agent_id, "a.txt", "a.txt", 5, "0" * 64, str(path), 0.0),
        )

    asyncio.run(_with_db(settings, action))
    return file_id


def insert_running_upload(settings: Settings, agent_id: str) -> tuple[str, str, str]:
    """造一个"传到一半"的任务 + upload + staging 文件，用于验证收尾。"""
    task_id = uuid.uuid4().hex[:16]
    upload_id = uuid.uuid4().hex
    staging = settings.staging_dir / f"{upload_id}.part"
    staging.parent.mkdir(parents=True, exist_ok=True)
    with open(staging, "wb") as handle:
        handle.truncate(1024)

    async def action(db: Database, _auth: AuthService) -> None:
        now = 0.0
        await db.execute(
            "INSERT INTO tasks (id, agent_id, type, src_path, status, upload_id,"
            " created_at, updated_at) VALUES (?, ?, 'fetch', ?, 'uploading', ?, ?, ?)",
            (task_id, agent_id, "big.bin", upload_id, now, now),
        )
        await db.execute(
            "INSERT INTO uploads (id, task_id, agent_id, filename, rel_path, total_size,"
            " chunk_size, total_chunks, received_json, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 'uploading', ?, ?)",
            (upload_id, task_id, agent_id, "big.bin", "big.bin", 1024, 1024, 1, now, now),
        )

    asyncio.run(_with_db(settings, action))
    return task_id, upload_id, str(staging)


# --- 删除节点 -----------------------------------------------------------


def test_delete_removes_node_and_invalidates_token(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        agent = enroll(client)
        login(client)

        response = client.delete(f"/api/v1/agents/{agent['agent_id']}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["agent_id"] == agent["agent_id"]
        assert client.get("/api/v1/agents").json()["total"] == 0

        # 被删掉的节点不能靠"自助轮换"把自己救回来
        rotated = client.post(
            "/internal/enroll",
            json={"agent_id": agent["agent_id"], "old_token": agent["token"]},
        )
        assert rotated.status_code == 401

    assert authenticate(settings, agent["token"]) is False


def test_delete_keeps_files_by_default(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        agent = enroll(client)
        file_id = insert_file(settings, agent["agent_id"], with_disk_file=True)
        login(client)

        body = client.delete(f"/api/v1/agents/{agent['agent_id']}").json()

        assert body["files_kept"] == 1
        assert body["files_trashed"] == 0

        files = client.get("/api/v1/files").json()["items"]
        assert [f["id"] for f in files] == [file_id]
        assert files[0]["deleted_at"] is None

    # 磁盘上的文件也还在
    assert (settings.files_dir / agent["agent_id"] / file_id[:2] / file_id).exists()


def test_delete_with_files_moves_them_to_trash(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        agent = enroll(client)
        file_id = insert_file(settings, agent["agent_id"], with_disk_file=True)
        login(client)

        body = client.delete(
            f"/api/v1/agents/{agent['agent_id']}", params={"delete_files": True}
        ).json()

        assert body["files_trashed"] == 1
        assert body["files_kept"] == 0

        # 只是进回收站，还能还原
        trash = client.get("/api/v1/files", params={"deleted": True}).json()["items"]
        assert [f["id"] for f in trash] == [file_id]
        assert client.post(f"/api/v1/files/{file_id}/restore").status_code == 200

    assert (settings.trash_dir / file_id).exists() is False  # 已还原回 files/


def test_delete_cancels_running_tasks_and_drops_staging(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        agent = enroll(client)
        task_id, _upload_id, staging = insert_running_upload(settings, agent["agent_id"])
        login(client)

        body = client.delete(f"/api/v1/agents/{agent['agent_id']}").json()

        assert body["tasks_canceled"] == 1

        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["status"] == "canceled"

    # staging 不清就是永久占盘的孤儿文件
    from pathlib import Path

    assert Path(staging).exists() is False


def test_delete_unknown_node_is_404(tmp_path):
    _settings, app = make_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        assert client.delete("/api/v1/agents/nope").status_code == 404


def test_admin_endpoints_require_session(tmp_path):
    _settings, app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/agents/pending").status_code == 401
        assert client.delete("/api/v1/agents/whatever").status_code == 401
        assert client.post("/api/v1/agents/pending/x/approve").status_code == 401


# --- 待批准接入 ---------------------------------------------------------


def test_approve_from_web_issues_token_once(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="closed")
    with TestClient(app) as client:
        login(client)
        pending_id = enroll(client, "lab-02", root_path="/srv/share")["pending_id"]

        listing = client.get("/api/v1/agents/pending").json()
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == pending_id
        assert listing["items"][0]["root_path"] == "/srv/share"

        approved = client.post(f"/api/v1/agents/pending/{pending_id}/approve")
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        claimed = client.get(f"/internal/enroll/{pending_id}")
        assert claimed.status_code == 200
        assert claimed.json()["token"].startswith("fba_")

        # 批完之后列表里是节点，待批准清空
        agents = client.get("/api/v1/agents").json()["items"]
        assert [a["name"] for a in agents] == ["lab-02"]
        assert agents[0]["files"] == 0
        assert client.get("/api/v1/agents/pending").json()["total"] == 0

        # token 只能领一次
        assert client.get(f"/internal/enroll/{pending_id}").status_code == 409


def test_reject_creates_no_agent(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="closed")
    with TestClient(app) as client:
        login(client)
        pending_id = enroll(client, "lab-03")["pending_id"]

        rejected = client.post(f"/api/v1/agents/pending/{pending_id}/reject")
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"

        assert client.get(f"/internal/enroll/{pending_id}").status_code == 403
        assert client.get("/api/v1/agents").json()["total"] == 0
        assert client.get("/api/v1/agents/pending").json()["total"] == 0


def test_deciding_twice_is_409(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="closed")
    with TestClient(app) as client:
        login(client)
        pending_id = enroll(client, "lab-04")["pending_id"]

        assert client.post(f"/api/v1/agents/pending/{pending_id}/approve").status_code == 200
        assert client.post(f"/api/v1/agents/pending/{pending_id}/reject").status_code == 409
        assert client.post("/api/v1/agents/pending/unknown/approve").status_code == 404


def test_pending_event_is_published(tmp_path):
    """页面开着的时候，新申请要能自己冒出来，而不是等管理员手动刷新。"""
    _settings, app = make_app(tmp_path, enroll_policy="closed")
    with TestClient(app) as client:
        login(client)
        queue = client.app.state.ctx.bus.subscribe()
        try:
            pending_id = enroll(client, "lab-05")["pending_id"]
            events = asyncio.run(_drain(queue))
        finally:
            client.app.state.ctx.bus.unsubscribe(queue)

    assert any(
        e["type"] == "enroll_pending" and e["data"]["pending_id"] == pending_id for e in events
    )


async def _drain(queue) -> list[dict]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items
