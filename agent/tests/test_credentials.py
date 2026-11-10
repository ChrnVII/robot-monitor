"""本地凭据：按 A 分槽存、读写、权限、损坏容错、旧布局迁移。

这个文件等价于一份长期口令，所以权限和"读坏了要能被忽略"这两点都要测。
分槽的部分测的是**换地址之后不会被当成陌生机器**，以及**换回去还是老身份**。
"""

from __future__ import annotations

import json
import os
import pathlib

from robot_monitor_agent.config import AgentConfig
from robot_monitor_agent.credentials import (
    Credentials,
    clear,
    current_path,
    default_state_dir,
    known_servers,
    legacy_path,
    load,
    same_server,
    save,
    slot_path,
)


def make_config(tmp_path, **kwargs) -> AgentConfig:
    return AgentConfig(state_dir=str(tmp_path / "state"), **kwargs)


def creds(server: str, token: str = "fba_x", agent_id: str = "a1", **kwargs) -> Credentials:
    return Credentials(server=server, token=token, agent_id=agent_id, **kwargs)


# --- 基本的读写 ---------------------------------------------------------


def test_save_then_load_roundtrip(tmp_path):
    config = make_config(tmp_path, server="https://a.example.com")
    saved = creds("https://a.example.com", root="/srv/share", name="lab-01")

    path = save(config, saved)
    assert path.exists()
    assert load(config) == saved


def test_load_without_server_uses_current(tmp_path):
    """不带 --server 启动时，用最近一次成功的那台。"""
    save(make_config(tmp_path), creds("https://a.example.com", token="fba_a"))
    save(make_config(tmp_path), creds("https://b.example.com", token="fba_b"))

    assert load(make_config(tmp_path)).token == "fba_b"


def test_slot_file_is_owner_only(tmp_path):
    config = make_config(tmp_path)
    path = save(config, creds("https://a"))

    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    else:
        assert path.exists()


def test_missing_returns_none(tmp_path):
    assert load(make_config(tmp_path, server="https://a")) is None
    assert load(make_config(tmp_path)) is None


def test_corrupted_slot_is_ignored(tmp_path):
    config = make_config(tmp_path, server="https://a")
    path = slot_path(config, "https://a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert load(config) is None


def test_incomplete_slot_is_ignored(tmp_path):
    config = make_config(tmp_path, server="https://a")
    path = slot_path(config, "https://a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server": "https://a"}), encoding="utf-8")

    assert load(config) is None


# --- 分槽：这是本次改动的核心 -------------------------------------------


def test_two_servers_do_not_overwrite_each_other(tmp_path):
    """换 A 不动另一台 A 的凭据，切回去还是原来那个身份。"""
    config_a = make_config(tmp_path, server="https://a.example.com")
    config_b = make_config(tmp_path, server="https://b.example.com")

    save(config_a, creds("https://a.example.com", token="fba_a", agent_id="id-a", root="/srv/a"))
    save(config_b, creds("https://b.example.com", token="fba_b", agent_id="id-b", root="/srv/b"))

    assert load(config_a).token == "fba_a"
    assert load(config_a).agent_id == "id-a"
    assert load(config_a).root == "/srv/a"
    assert load(config_b).token == "fba_b"
    assert load(config_b).root == "/srv/b"


def test_unknown_server_does_not_borrow_another_token(tmp_path):
    """带着新地址启动时绝不能拿旧 A 的 token 去连 —— 那只会换来一个 4401。"""
    save(make_config(tmp_path), creds("https://old.example.com", token="fba_old"))

    assert load(make_config(tmp_path, server="https://new.example.com")) is None


def test_same_server_different_spellings_share_one_slot(tmp_path):
    config = make_config(tmp_path)
    forms = [
        "https://a.example.com",
        "https://a.example.com/",
        "wss://a.example.com/ws/agent",
    ]
    paths = {slot_path(config, form) for form in forms}
    assert len(paths) == 1
    assert all(same_server(forms[0], form) for form in forms)

    save(make_config(tmp_path, server=forms[0]), creds(forms[0], token="fba_a"))
    assert load(make_config(tmp_path, server=forms[2])).token == "fba_a"


def test_multiple_slots_without_current_refuses_to_guess(tmp_path):
    """有多个槽又没有指针时返回 None，由调用方要求显式 --server。"""
    save(make_config(tmp_path), creds("https://a.example.com", token="fba_a"))
    save(make_config(tmp_path), creds("https://b.example.com", token="fba_b"))
    current_path(make_config(tmp_path)).unlink()

    assert load(make_config(tmp_path)) is None
    assert known_servers(make_config(tmp_path)) == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_single_slot_still_loads_without_current(tmp_path):
    """current 丢了但只有一个槽，还是能用 —— 手工拷贝目录时常见。"""
    save(make_config(tmp_path), creds("https://a.example.com", token="fba_a"))
    current_path(make_config(tmp_path)).unlink()

    assert load(make_config(tmp_path)).token == "fba_a"


def test_clear_only_forgets_the_named_server(tmp_path):
    save(make_config(tmp_path), creds("https://a.example.com", token="fba_a"))
    save(make_config(tmp_path), creds("https://b.example.com", token="fba_b"))

    clear(make_config(tmp_path, server="https://b.example.com"))

    assert load(make_config(tmp_path, server="https://b.example.com")) is None
    assert load(make_config(tmp_path, server="https://a.example.com")).token == "fba_a"


def test_clear_without_server_forgets_current(tmp_path):
    save(make_config(tmp_path), creds("https://a.example.com", token="fba_a"))
    clear(make_config(tmp_path))

    assert load(make_config(tmp_path)) is None
    assert not current_path(make_config(tmp_path)).exists()


# --- 旧布局迁移 ---------------------------------------------------------


def _write_legacy(config: AgentConfig, payload: dict) -> pathlib.Path:
    path = legacy_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_legacy_file_is_still_readable(tmp_path):
    """升级之后第一次启动，还没写过槽，得能读旧文件。"""
    config = make_config(tmp_path)
    _write_legacy(
        config,
        {"server": "https://a.example.com", "token": "fba_old", "agent_id": "id-a"},
    )

    assert load(make_config(tmp_path, server="https://a.example.com")).token == "fba_old"
    assert load(make_config(tmp_path)).token == "fba_old"


def test_legacy_file_is_migrated_on_save(tmp_path):
    config = make_config(tmp_path)
    legacy = _write_legacy(
        config, {"server": "https://a.example.com", "token": "fba_old", "agent_id": "id-a"}
    )

    save(config, creds("https://b.example.com", token="fba_new"))

    assert not legacy.exists(), "迁移之后旧文件要删掉，同一个 token 不留两份"
    assert load(make_config(tmp_path, server="https://a.example.com")).token == "fba_old"
    assert load(make_config(tmp_path, server="https://b.example.com")).token == "fba_new"


def test_corrupted_legacy_file_is_left_alone(tmp_path):
    """解析不了就不迁也不删 —— 那可能是唯一的凭据，留给人看。"""
    config = make_config(tmp_path)
    legacy = _write_legacy(config, {"server": "https://a"})
    legacy.write_text("{broken", encoding="utf-8")

    save(config, creds("https://b.example.com", token="fba_new"))

    assert legacy.exists()


# --- 目录解析 -----------------------------------------------------------


def test_state_dir_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOT_MONITOR_AGENT_STATE_DIR", str(tmp_path / "custom"))
    assert default_state_dir() == tmp_path / "custom"


def test_default_lands_in_project_dir():
    """默认凭据目录在项目内（agent/.robot-monitor/），由 .gitignore 挡住版本库。"""
    import robot_monitor_agent.credentials as credentials_module

    project_root = pathlib.Path(credentials_module.__file__).resolve().parents[2]
    assert default_state_dir() == project_root / ".robot-monitor"


def test_installed_without_project_falls_back_to_xdg(tmp_path, monkeypatch):
    """wheel 安装后包在 site-packages 里，向上找不到 pyproject.toml，回落 XDG。"""
    import robot_monitor_agent.credentials as credentials_module

    monkeypatch.delenv("ROBOT_MONITOR_AGENT_STATE_DIR", raising=False)
    monkeypatch.setattr(
        credentials_module,
        "__file__",
        str(tmp_path / "lib" / "site-packages" / "robot_monitor_agent" / "credentials.py"),
    )

    assert default_state_dir() == pathlib.Path.home() / ".local" / "state" / "robot-monitor"
