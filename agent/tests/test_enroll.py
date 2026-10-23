"""设备注册流程（B 端）。

用 `httpx.MockTransport` 模拟 A 的三种返回：直接放行、待批准、明确拒绝。
把 POLL_INTERVAL 压到毫秒级，这样"等待批准"也能在测试里真实走一遍。
"""

from __future__ import annotations

import httpx
import pytest

from filebridge_agent import enroll as enroll_module
from filebridge_agent.config import AgentConfig, ConfigError
from filebridge_agent.credentials import Credentials, load, save
from filebridge_agent.enroll import EnrollError, ensure_credentials, rotate


def make_config(tmp_path, **kwargs) -> AgentConfig:
    return AgentConfig(
        server="http://127.0.0.1:8000",
        root=str(tmp_path / "share"),
        state_dir=str(tmp_path / "state"),
        **kwargs,
    )


def install_transport(monkeypatch, handler):
    monkeypatch.setattr(
        enroll_module,
        "_client",
        lambda config: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8000"
        ),
    )


def test_enroll_ok_persists_credentials(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/enroll"
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "fba_new"})

    install_transport(monkeypatch, handler)

    resolved = await_ensure(config)

    assert resolved.token == "fba_new"
    assert resolved.agent_id == "a1"
    stored = load(config)
    assert stored is not None
    assert stored.token == "fba_new"


def test_existing_credentials_skip_enrollment(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    save(config, Credentials(server="http://127.0.0.1:8000", token="fba_old", agent_id="a1"))

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("有凭据时不该再请求 A")

    install_transport(monkeypatch, handler)
    fresh = AgentConfig(root=str(tmp_path / "share"), state_dir=str(tmp_path / "state"))

    resolved = await_ensure(fresh)

    assert resolved.token == "fba_old"
    assert resolved.server == "http://127.0.0.1:8000"


def test_pending_then_approved(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    state = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"pending_id": "p" * 8, "hint": "approve p"})
        state["get"] += 1
        if state["get"] == 1:
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(200, json={"status": "ok", "agent_id": "a2", "token": "fba_after"})

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(enroll_module, "POLL_INTERVAL", 0.01)

    resolved = await_ensure(config)

    assert resolved.token == "fba_after"
    assert state["get"] == 2


def test_rejected_enrollment_is_reported(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"pending_id": "p" * 8})
        return httpx.Response(403, json={"code": "forbidden", "message": "管理员已拒绝"})

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(enroll_module, "POLL_INTERVAL", 0.01)

    with pytest.raises(EnrollError, match="拒绝"):
        await_ensure(config)


def test_server_error_is_reported(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": "internal", "message": "boom"})

    install_transport(monkeypatch, handler)

    with pytest.raises(EnrollError, match="500"):
        await_ensure(config)


def test_missing_server_is_reported(tmp_path):
    config = AgentConfig(root=str(tmp_path), state_dir=str(tmp_path / "state"))

    with pytest.raises(ConfigError, match="server"):
        await_ensure(config)


def test_missing_root_is_reported(tmp_path, monkeypatch):
    config = AgentConfig(server="http://127.0.0.1:8000", state_dir=str(tmp_path / "state"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "fba_x"})

    install_transport(monkeypatch, handler)

    with pytest.raises(ConfigError, match="root"):
        await_ensure(config)


def test_rotate_updates_local_credentials(tmp_path, monkeypatch):
    config = make_config(tmp_path, token="fba_old", agent_id="a1")
    save(config, Credentials(server=config.server, token="fba_old", agent_id="a1"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "agent_id": "a1", "token": "fba_rotated"})

    install_transport(monkeypatch, handler)

    token = await_rotate(config)

    assert token == "fba_rotated"
    stored = load(config)
    assert stored is not None and stored.token == "fba_rotated"


def test_rotate_returns_none_when_rejected(tmp_path, monkeypatch):
    config = make_config(tmp_path, token="fba_old", agent_id="a1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "unauthorized", "message": "旧 token 无效"})

    install_transport(monkeypatch, handler)

    assert await_rotate(config) is None


def test_rotate_without_credentials_does_nothing(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("没有凭据时不该发请求")

    install_transport(monkeypatch, handler)

    assert await_rotate(config) is None


def await_ensure(config: AgentConfig) -> AgentConfig:
    import asyncio

    return asyncio.run(ensure_credentials(config))


def await_rotate(config: AgentConfig) -> str | None:
    import asyncio

    return asyncio.run(rotate(config))
