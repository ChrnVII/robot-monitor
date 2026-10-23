"""鉴权测试。

重点不是覆盖率，而是三条不可退让的性质：

1. 库里不存明文 token / 密码
2. 会话过期立刻失效（不是"过期后还能用一会儿"）
3. 吊销 agent 后旧 token 立即被拒
"""

from __future__ import annotations

import time

import pytest

from filebridge_server.auth import AuthService, hash_password, hash_token
from filebridge_server.config import Settings
from filebridge_server.db import Database

PASSWORD = "correct-horse-battery"


async def make_service(tmp_path, *, password_hash: str | None = None) -> AuthService:
    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        admin_password_hash=password_hash if password_hash is not None else hash_password(PASSWORD),
    )
    settings.ensure_dirs()
    db = Database(settings.db_path)
    await db.connect()
    return AuthService(db, settings)


async def test_login_returns_session_token(tmp_path):
    auth = await make_service(tmp_path)

    token = await auth.login("admin", PASSWORD)
    assert token
    session = await auth.verify_session(token)
    assert session is not None
    assert session.expires_at > time.time()

    await auth.db.close()


async def test_wrong_password_is_rejected(tmp_path):
    auth = await make_service(tmp_path)

    assert await auth.login("admin", "wrong") is None
    assert await auth.login("root", PASSWORD) is None

    await auth.db.close()


async def test_missing_password_hash_disables_login(tmp_path):
    auth = await make_service(tmp_path, password_hash="")

    assert await auth.login("admin", PASSWORD) is None

    await auth.db.close()


async def test_session_token_is_not_stored_in_plaintext(tmp_path):
    auth = await make_service(tmp_path)
    token = await auth.login("admin", PASSWORD)

    row = await auth.db.fetchone("SELECT token_hash FROM sessions")
    assert row is not None
    assert row["token_hash"] != token
    assert row["token_hash"] == hash_token(token)

    await auth.db.close()


async def test_expired_session_is_rejected(tmp_path):
    auth = await make_service(tmp_path)
    token = await auth.login("admin", PASSWORD)

    # 直接把过期时间改到过去，模拟 TTL 到期
    await auth.db.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
    assert await auth.verify_session(token) is None

    await auth.db.close()


async def test_logout_invalidates_session_immediately(tmp_path):
    auth = await make_service(tmp_path)
    token = await auth.login("admin", PASSWORD)

    await auth.logout(token)
    assert await auth.verify_session(token) is None

    await auth.db.close()


async def test_agent_token_roundtrip_and_revocation(tmp_path):
    auth = await make_service(tmp_path)

    agent_id, token = await auth.issue_agent_token("b-office", hostname="desk")
    row = await auth.authenticate_agent(token)
    assert row is not None
    assert row["id"] == agent_id

    # 库里只有哈希
    stored = await auth.db.fetchval("SELECT token_hash FROM agents WHERE id = ?", (agent_id,))
    assert stored != token
    assert stored == hash_token(token)

    assert await auth.revoke_agent(agent_id) is True
    assert await auth.authenticate_agent(token) is None

    await auth.db.close()


async def test_agent_token_lookup_rejects_garbage(tmp_path):
    auth = await make_service(tmp_path)

    assert await auth.authenticate_agent(None) is None
    assert await auth.authenticate_agent("") is None
    assert await auth.authenticate_agent("not-a-token") is None
    assert await auth.authenticate_agent("fba_" + "x" * 40) is None

    await auth.db.close()


async def test_reissue_token_replaces_old_one(tmp_path):
    auth = await make_service(tmp_path)

    agent_id, first = await auth.issue_agent_token("b-office")
    same_id, second = await auth.issue_agent_token("b-office")

    assert same_id == agent_id
    assert await auth.authenticate_agent(first) is None
    assert await auth.authenticate_agent(second) is not None

    await auth.db.close()


async def test_audit_log_records_actions(tmp_path):
    auth = await make_service(tmp_path)
    await auth.login("admin", PASSWORD)

    entries = await auth.recent_audit(limit=10)
    assert any(entry["action"] == "auth.login" for entry in entries)

    await auth.db.close()


@pytest.mark.parametrize("password", ["short", "中文密码也可以", "x" * 200])
def test_password_hash_roundtrip(password):
    digest = hash_password(password)
    from filebridge_server.auth import verify_password

    assert verify_password(password, digest)
    assert not verify_password(password + "x", digest)


# --- 密码入库后的行为 -----------------------------------------------------


async def test_password_can_be_set_in_db(tmp_path):
    auth = await make_service(tmp_path, password_hash="")

    assert await auth.password_configured() is False
    await auth.set_password("admin", PASSWORD)
    assert await auth.password_configured() is True
    assert await auth.login("admin", PASSWORD) is not None

    await auth.db.close()


async def test_db_password_wins_over_env(tmp_path):
    """env 和库里都有密码时，必须用库里的。

    否则改完密码发现 env 里的旧哈希还在生效——这是最难自查的一类问题。
    """
    auth = await make_service(tmp_path)  # env hash = PASSWORD
    await auth.set_password("admin", "a-brand-new-password")

    assert await auth.login("admin", PASSWORD) is None
    assert await auth.login("admin", "a-brand-new-password") is not None

    await auth.db.close()


async def test_adopt_env_password_moves_hash_into_db(tmp_path):
    auth = await make_service(tmp_path)

    assert await auth.adopt_env_password() is True
    row = await auth.db.fetchone("SELECT username, password_hash FROM admin_user WHERE id = 1")
    assert row["username"] == "admin"
    assert row["password_hash"].startswith("$argon2")

    assert await auth.adopt_env_password() is False  # 幂等
    assert await auth.login("admin", PASSWORD) is not None

    await auth.db.close()


async def test_change_password_invalidates_existing_sessions(tmp_path):
    auth = await make_service(tmp_path)
    token = await auth.login("admin", PASSWORD)
    assert await auth.verify_session(token) is not None

    assert await auth.change_password(PASSWORD, "new-password-1234") is True

    assert await auth.verify_session(token) is None
    assert await auth.login("admin", PASSWORD) is None
    assert await auth.login("admin", "new-password-1234") is not None

    await auth.db.close()


async def test_change_password_rejects_wrong_current(tmp_path):
    auth = await make_service(tmp_path)

    assert await auth.change_password("not-the-password", "whatever-1234") is False
    assert await auth.login("admin", PASSWORD) is not None

    await auth.db.close()


async def test_initial_password_file_is_removed_on_login(tmp_path):
    auth = await make_service(tmp_path, password_hash="")
    await auth.set_password("admin", PASSWORD)

    marker = tmp_path / "data" / "initial-password"
    marker.write_text("temp\n", encoding="utf-8")

    await auth.login("admin", PASSWORD)
    assert not marker.exists()

    await auth.db.close()
