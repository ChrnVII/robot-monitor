"""设备注册端点的准入策略。

这个端点不需要 token，所以它是整个系统里唯一"对外敞开"的入口。测试要钉住的是
准入判据本身：什么情况下自动放行、什么情况下必须人工、以及被批准后 token 只能
被领走一次。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from filebridge_server.auth import AuthService, hash_password
from filebridge_server.config import Settings
from filebridge_server.db import Database
from filebridge_server.main import create_app

PASSWORD = "enroll-test-123"


def make_app(tmp_path, **overrides):
    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        admin_password_hash=hash_password(PASSWORD),
        serve_frontend=False,
        **overrides,
    )
    return settings, create_app(settings)


def enroll(client: TestClient, **payload):
    return client.post("/internal/enroll", json=payload)


async def _with_auth(settings: Settings, action):
    db = Database(settings.db_path)
    await db.connect()
    try:
        return await action(AuthService(db, settings))
    finally:
        await db.close()


def approve(settings: Settings, pending_id: str) -> bool:
    return asyncio.run(
        _with_auth(settings, lambda auth: auth.set_enroll_status(pending_id, "approved"))
    )


def reject(settings: Settings, pending_id: str) -> bool:
    return asyncio.run(
        _with_auth(settings, lambda auth: auth.set_enroll_status(pending_id, "rejected"))
    )


def authenticate(settings: Settings, token: str) -> bool:
    return asyncio.run(_with_auth(settings, lambda auth: auth.authenticate_agent(token))) is not None


def test_default_policy_requires_approval_even_for_the_first_device(tmp_path):
    """默认 closed：第一台也要管理员在网页上点批准。

    这条钉住的是"默认安全"。之前默认是 first（首台免审），等于把
    "部署完成→第一台 B 连上"这段窗口交给了任何能连到 A 的机器。
    """
    settings, app = make_app(tmp_path)
    with TestClient(app) as client:
        response = enroll(client, hostname="lab-01", root_path="/srv/share")

        assert response.status_code == 202, response.text
        assert response.json()["status"] == "pending"

        pending_id = response.json()["pending_id"]
        assert approve(settings, pending_id) is True
        token = client.get(f"/internal/enroll/{pending_id}").json()["token"]
        assert token.startswith("fba_")

    assert authenticate(settings, token)


def test_first_device_enrolls_without_approval(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        response = enroll(client, hostname="lab-01", root_path="/srv/share")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token"].startswith("fba_")
        assert body["agent_id"]

    assert authenticate(settings, body["token"])


def test_second_device_goes_to_pending_under_first_policy(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        assert enroll(client, hostname="lab-01").status_code == 200

        second = enroll(client, hostname="lab-02")

        assert second.status_code == 202
        pending_id = second.json()["pending_id"]
        assert "approve" in second.json()["hint"]
        assert client.get(f"/internal/enroll/{pending_id}").json()["status"] == "pending"


def test_open_policy_enrolls_everyone(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        first = enroll(client, hostname="lab-01")
        second = enroll(client, hostname="lab-02")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["agent_id"] != second.json()["agent_id"]


def test_closed_policy_requires_approval_even_for_the_first(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="closed")
    with TestClient(app) as client:
        response = enroll(client, hostname="lab-01")
        assert response.status_code == 202

        pending_id = response.json()["pending_id"]
        assert approve(settings, pending_id) is True

        claimed = client.get(f"/internal/enroll/{pending_id}")
        assert claimed.status_code == 200
        assert claimed.json()["token"].startswith("fba_")


def test_approved_request_can_be_claimed_only_once(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        enroll(client, hostname="lab-01")
        pending_id = enroll(client, hostname="lab-02").json()["pending_id"]

        approve(settings, pending_id)

        first = client.get(f"/internal/enroll/{pending_id}")
        assert first.status_code == 200
        assert first.json()["token"].startswith("fba_")

        # 再来一次就不给了——否则任何知道 pending_id 的人都能拿到 token
        second = client.get(f"/internal/enroll/{pending_id}")
        assert second.status_code == 409


def test_rejected_request_is_forbidden_on_claim(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        enroll(client, hostname="lab-01")
        pending_id = enroll(client, hostname="lab-02").json()["pending_id"]

        assert reject(settings, pending_id) is True
        assert client.get(f"/internal/enroll/{pending_id}").status_code == 403


def test_unknown_pending_id_is_404(tmp_path):
    _settings, app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/internal/enroll/does-not-exist").status_code == 404


def test_pending_request_never_stores_the_token(tmp_path):
    """待办表不该成为凭据的第二个存放点。"""
    settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        enroll(client, hostname="lab-01")
        pending_id = enroll(client, hostname="lab-02").json()["pending_id"]

    async def dump():
        db = Database(settings.db_path)
        await db.connect()
        try:
            return [dict(row) for row in await db.fetchall("SELECT * FROM enroll_requests")]
        finally:
            await db.close()

    rows = asyncio.run(dump())
    assert any(row["id"] == pending_id for row in rows)
    for row in rows:
        assert not any("fba_" in str(value) for value in row.values())


def test_rotate_with_old_token(tmp_path):
    settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        first = enroll(client, hostname="lab-01").json()

        rotated = client.post(
            "/internal/enroll",
            json={"agent_id": first["agent_id"], "old_token": first["token"]},
        )

        assert rotated.status_code == 200
        new_token = rotated.json()["token"]
        assert new_token != first["token"]

    assert authenticate(settings, new_token) is True
    assert authenticate(settings, first["token"]) is False


def test_rotate_with_invalid_token_is_rejected(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="first")
    with TestClient(app) as client:
        first = enroll(client, hostname="lab-01").json()

        response = client.post(
            "/internal/enroll",
            json={"agent_id": first["agent_id"], "old_token": "fba_not-a-real-token"},
        )

        assert response.status_code == 401


def test_rotate_cannot_target_another_agent(tmp_path):
    _settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        first = enroll(client, hostname="lab-01").json()
        second = enroll(client, hostname="lab-02").json()

        response = client.post(
            "/internal/enroll",
            json={"agent_id": first["agent_id"], "old_token": second["token"]},
        )

        assert response.status_code == 401


def test_duplicate_hostname_does_not_steal_existing_node(tmp_path):
    """两台机器同名时，必须并存，而不是把前一台的 token 换掉。"""
    settings, app = make_app(tmp_path, enroll_policy="open")
    with TestClient(app) as client:
        first = enroll(client, hostname="lab").json()
        second = enroll(client, hostname="lab").json()

        assert first["agent_id"] != second["agent_id"]
        assert client.get("/api/v1/agents").status_code == 401  # 没登录，确认受保护

    assert authenticate(settings, first["token"])
    assert authenticate(settings, second["token"])


@pytest.mark.parametrize("policy", ["first", "open", "closed"])
def test_policy_is_persisted_in_settings(tmp_path, policy):
    settings, _app = make_app(tmp_path, enroll_policy=policy)
    assert settings.enroll_policy == policy
