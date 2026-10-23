"""B 侧的本地凭据文件。

注册一次之后，server / token / agent_id / root 都落在这里，之后启动不需要任何参数。
文件权限 0600 —— 它等价于一份长期口令，不能让同组用户读到。

位置：`$FILEBRIDGE_AGENT_STATE_DIR` > `$XDG_STATE_HOME/filebridge` >
`~/.local/state/filebridge`。用 state 而不是 config，因为这是运行时产生的、
可再生的东西（删掉重新注册即可）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import AgentConfig

logger = logging.getLogger("filebridge.agent.state")

STATE_FILENAME = "agent.json"


@dataclass
class Credentials:
    server: str
    token: str
    agent_id: str
    root: str | None = None
    name: str | None = None


def default_state_dir() -> Path:
    explicit = os.environ.get("FILEBRIDGE_AGENT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "filebridge"


def state_path(config: AgentConfig) -> Path:
    if config.state_dir:
        return Path(config.state_dir).expanduser() / STATE_FILENAME
    return default_state_dir() / STATE_FILENAME


def load(config: AgentConfig) -> Credentials | None:
    path = state_path(config)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读不到凭据文件 %s：%s", path, exc)
        return None

    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("凭据文件不是合法 JSON，忽略：%s", path)
        return None

    token = data.get("token")
    server = data.get("server")
    agent_id = data.get("agent_id")
    if not isinstance(token, str) or not isinstance(server, str) or not isinstance(agent_id, str):
        logger.warning("凭据文件缺少必要字段，忽略：%s", path)
        return None

    return Credentials(
        server=server,
        token=token,
        agent_id=agent_id,
        root=data.get("root") if isinstance(data.get("root"), str) else None,
        name=data.get("name") if isinstance(data.get("name"), str) else None,
    )


def save(config: AgentConfig, credentials: Credentials) -> Path:
    path = state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(asdict(credentials), ensure_ascii=False, indent=2) + "\n"
    # 用 os.open 指定 0600 创建，避免"先建成 644、再 chmod"中间那个窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)

    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - 非 POSIX 文件系统
        pass
    return path


def clear(config: AgentConfig) -> None:
    try:
        state_path(config).unlink(missing_ok=True)
    except OSError:
        pass
