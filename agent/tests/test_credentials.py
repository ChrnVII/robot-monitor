"""本地凭据文件：读写、权限、损坏容错。

这个文件等价于一份长期口令，所以权限和"读坏了要能被忽略"这两点都要测。
"""

from __future__ import annotations

import json
import os

from filebridge_agent.config import AgentConfig
from filebridge_agent.credentials import (
    Credentials,
    default_state_dir,
    load,
    save,
    state_path,
)


def make_config(tmp_path, **kwargs) -> AgentConfig:
    return AgentConfig(state_dir=str(tmp_path / "state"), **kwargs)


def test_save_then_load_roundtrip(tmp_path):
    config = make_config(tmp_path)
    saved = Credentials(
        server="https://a.example.com",
        token="fba_abc",
        agent_id="agent1",
        root="/srv/share",
        name="lab-01",
    )

    path = save(config, saved)
    assert path.exists()
    assert load(config) == saved


def test_state_file_is_owner_only(tmp_path):
    config = make_config(tmp_path)
    path = save(config, Credentials(server="https://a", token="fba_x", agent_id="a1"))

    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        assert path.exists()


def test_missing_file_returns_none(tmp_path):
    assert load(make_config(tmp_path)) is None


def test_corrupted_file_is_ignored(tmp_path):
    config = make_config(tmp_path)
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert load(config) is None


def test_incomplete_file_is_ignored(tmp_path):
    config = make_config(tmp_path)
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"server": "https://a"}), encoding="utf-8")

    assert load(config) is None


def test_state_dir_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FILEBRIDGE_AGENT_STATE_DIR", str(tmp_path / "custom"))
    assert default_state_dir() == tmp_path / "custom"


def test_xdg_state_home_is_respected(tmp_path, monkeypatch):
    monkeypatch.delenv("FILEBRIDGE_AGENT_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    assert default_state_dir() == tmp_path / "xdg" / "filebridge"
