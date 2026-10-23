"""agent 配置。

优先级：命令行参数 > 环境变量 > TOML 文件 > 默认值。

`--server` 允许两种写法，因为部署时人记不住该写哪个：

- `https://files.example.com`（推荐）——由它推出 `wss://.../ws/agent`
- `wss://files.example.com/ws/agent`——已经写全了就直接用

两种写法都必须能推出 HTTP base，否则上传端点会拼错。这是本文件里唯一有点
绕的地方，所以单独测了（`tests/test_config.py`）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

WS_PATH = "/ws/agent"
ENV_PREFIX = "FILEBRIDGE_AGENT_"


class ConfigError(Exception):
    pass


@dataclass
class AgentConfig:
    server: str = ""
    token: str = ""
    root: str = ""
    name: str | None = None
    heartbeat_interval: float = 15.0
    max_concurrent_uploads: int = 3
    verify_tls: bool = True
    # 本地凭据文件目录；不传则用 $XDG_STATE_HOME/filebridge 或 ~/.local/state/filebridge
    state_dir: str | None = None
    # 注册后由 A 分配，轮换 token 时要带上
    agent_id: str | None = None
    log_dir: str | None = None
    log_level: str = "INFO"
    open_timeout: float = 10.0
    http_timeout: float = 60.0

    def validate(self) -> None:
        """只校验"给了的值是否合法"。

        server / root / token 是否齐全不在这里判断——它们可以从本地凭据文件补，
        由 `enroll.ensure_credentials()` 统一解析并给出更准确的报错。
        """
        if self.max_concurrent_uploads < 1:
            raise ConfigError("max_concurrent_uploads 至少为 1")
        if self.server.strip():
            _ = self.http_base  # 协议写错要在这里就炸，而不是等到连的时候

    # --- 派生 URL -------------------------------------------------------

    @property
    def _parts(self):
        return urlsplit(self.server.strip())

    @property
    def ws_url(self) -> str:
        parts = self._parts
        if parts.scheme in ("ws", "wss"):
            if parts.path.rstrip("/").endswith(WS_PATH):
                return self.server.strip()
            return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}{WS_PATH}"
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}{self._base_path()}{WS_PATH}"

    @property
    def http_base(self) -> str:
        parts = self._parts
        scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
        if scheme not in ("http", "https"):
            raise ConfigError(f"server 的协议不对：{parts.scheme!r}，应为 http/https/ws/wss")
        return f"{scheme}://{parts.netloc}{self._base_path()}"

    def _base_path(self) -> str:
        path = self._parts.path.rstrip("/")
        path = path.removesuffix(WS_PATH)
        return path


def load_config(path: str | os.PathLike[str] | None = None, **overrides: object) -> AgentConfig:
    """读 TOML + 环境变量 + 显式覆盖，返回配置。"""
    data: dict[str, object] = {}

    if path:
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            raise ConfigError(f"配置文件不存在：{file_path}")
        with open(file_path, "rb") as handle:
            data.update(tomllib.load(handle))

    valid = {f.name for f in fields(AgentConfig)}
    for name in valid:
        env_value = os.environ.get(ENV_PREFIX + name.upper())
        if env_value is not None:
            data[name] = env_value

    for key, value in overrides.items():
        if value is not None and key in valid:
            data[key] = value

    # 环境变量是字符串，这里把数值/布尔字段转回来
    for key in ("heartbeat_interval", "open_timeout", "http_timeout"):
        if key in data and isinstance(data[key], str):
            data[key] = float(data[key])
    if "max_concurrent_uploads" in data and isinstance(data["max_concurrent_uploads"], str):
        data["max_concurrent_uploads"] = int(data["max_concurrent_uploads"])
    if "verify_tls" in data and isinstance(data["verify_tls"], str):
        data["verify_tls"] = data["verify_tls"].strip().lower() not in ("0", "false", "no")

    unknown = set(data) - valid
    if unknown:
        raise ConfigError(f"未知的配置项：{', '.join(sorted(unknown))}")

    return AgentConfig(**data)  # type: ignore[arg-type]
