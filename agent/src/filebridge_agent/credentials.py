"""B 侧的本地凭据，按 A 分开存。

**一个 A 一个文件。** 注册一次之后，server / token / agent_id / root 都落在
对应的槽里，之后启动不需要任何参数。

布局：

```
.filebridge/
  agents/
    files.example.com-a1b2c3d4.json     # 每个 A 一份，内容就是原来那份 agent.json
    backup.lan:8443-9f8e7d6c.json
  current                               # 最近一次成功使用的 A（规范化地址）
  agent.json                            # 旧布局，只用于升级时迁移
```

文件名带主机名是给人看的：`ls` 一眼就知道这台机器认过几个 A、分别是哪台。
真正决定"是不是同一个 A"的是后面那 8 位 hash——它算的是**规范化之后的地址**
（`http_base`：统一 http/https、去掉尾斜杠和 `/ws/agent`），所以
`https://a.com`、`https://a.com/`、`wss://a.com/ws/agent` 落在同一个槽里。

**为什么不是一个文件里放 `{"agents": {...}}`：** 那是读-改-写。systemd 重启
的窗口里正好有人手动跑了一次 `--server 另一个`，后写的会把先写的那份覆盖掉，
代价是另一台 A 上那台节点凭空消失，且没有任何报错。一槽一文件没有这个窗口，
删一个槽就是 `rm` 一个文件，运维直觉也不用改。

**这不等于同时连多台 A。** 一个进程仍然只连一台，这里解决的只是"记住去过哪几
台、下次别把自己当成陌生机器"。

文件权限 0600，目录 0700 —— 它们等价于一份长期口令，不能让同组用户读到。

位置：`$FILEBRIDGE_AGENT_STATE_DIR` > 项目内 `.filebridge/` >
`~/.local/state/filebridge`（wheel 安装、找不到项目根时的回落）。

按用户要求与代码放在一起（agent/.filebridge/），由 .gitignore 挡住版本库；
代价是 rsync / 备份 / 重装项目目录时要记得绕开它。这是有意的选择：
这个项目就两台自己的机器，目录不会被同步工具搬运。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import AgentConfig, ConfigError

logger = logging.getLogger("filebridge.agent.state")

SLOTS_DIRNAME = "agents"
CURRENT_FILENAME = "current"
LEGACY_FILENAME = "agent.json"

_UNSAFE = re.compile(r"[^A-Za-z0-9.-]")


@dataclass
class Credentials:
    server: str
    token: str
    agent_id: str
    root: str | None = None
    name: str | None = None
    # 这台 A 的身份。地址会变（换域名/IP），instance_id 不会，所以换地址时
    # 靠它认出"还是那一台"，而不是把人当成新机器重新注册一遍。
    instance_id: str | None = None


# --- 位置 ---------------------------------------------------------------


def default_state_dir() -> Path:
    explicit = os.environ.get("FILEBRIDGE_AGENT_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return _project_state_dir()


def _project_state_dir() -> Path:
    """项目内的 .filebridge/。找不到项目根（wheel 安装）时回落到 ~/.local/state。"""
    here = Path(__file__).resolve()
    for parent in here.parents[:6]:
        if (parent / "pyproject.toml").is_file():
            return parent / ".filebridge"
    return Path.home() / ".local" / "state" / "filebridge"


def store_dir(config: AgentConfig) -> Path:
    """凭据根目录。"""
    if config.state_dir:
        return Path(config.state_dir).expanduser()
    return default_state_dir()


def slots_dir(config: AgentConfig) -> Path:
    return store_dir(config) / SLOTS_DIRNAME


def current_path(config: AgentConfig) -> Path:
    return store_dir(config) / CURRENT_FILENAME


def legacy_path(config: AgentConfig) -> Path:
    """旧的单文件布局。只读：下一次 save 会把它迁进槽里然后删掉。"""
    return store_dir(config) / LEGACY_FILENAME


# --- 地址规范化 ---------------------------------------------------------


def canonical_server(server: str) -> str:
    """把同一个 A 的不同写法归一成同一个字符串。

    直接复用 `AgentConfig.http_base`，而不是在这里重写一遍 scheme / 尾斜杠的
    规则——那是配置层已经测过的东西（`tests/test_config.py`），抄一份出来
    迟早两边不一致。
    """
    text = (server or "").strip()
    if not text:
        return ""
    try:
        return AgentConfig(server=text).http_base
    except ConfigError:
        return text.rstrip("/")


def server_key(server: str) -> str:
    return hashlib.sha256(canonical_server(server).encode("utf-8")).hexdigest()[:8]


def same_server(left: str, right: str) -> bool:
    return canonical_server(left) == canonical_server(right)


def _slot_name(server: str) -> str:
    canonical = canonical_server(server)
    host = _UNSAFE.sub("_", canonical.split("://", 1)[-1])[:48] or "server"
    return f"{host}-{server_key(server)}.json"


def slot_path(config: AgentConfig, server: str) -> Path:
    return slots_dir(config) / _slot_name(server)


# --- 读写 ---------------------------------------------------------------


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读不到凭据文件 %s：%s", path, exc)
        return None


def _read(path: Path) -> Credentials | None:
    raw = _text(path)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("凭据文件不是合法 JSON，忽略：%s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("凭据文件不是一个对象，忽略：%s", path)
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
        instance_id=data.get("instance_id") if isinstance(data.get("instance_id"), str) else None,
    )


def _write_secret(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    # 用 os.open 指定 0600 创建，避免"先建成 644、再 chmod"中间那个窗口
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    _chmod(path, 0o600)


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - 非 POSIX 文件系统
        pass


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _current_server(config: AgentConfig) -> str | None:
    raw = _text(current_path(config))
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _slot_files(config: AgentConfig) -> list[Path]:
    directory = slots_dir(config)
    try:
        return sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return []


def stored_slots(config: AgentConfig) -> list[Credentials]:
    """所有能读出来的槽，按地址排序。损坏的那些直接跳过。"""
    stored = [_read(path) for path in _slot_files(config)]
    return [item for item in stored if item is not None]


def known_servers(config: AgentConfig) -> list[str]:
    """这台机器认过的所有 A 的地址（规范化之后）。给报错信息用，也用来自查。"""
    servers = [canonical_server(item.server) for item in stored_slots(config)]
    legacy = _read(legacy_path(config))
    if legacy is not None:
        servers.append(canonical_server(legacy.server))
    return sorted(dict.fromkeys(servers))


def load(config: AgentConfig) -> Credentials | None:
    """找出这次该用哪份凭据。

    顺序是"越明确越优先"，没有一步会猜：

    1. 命令行给了 `--server`：只认这个地址的槽。**不会**退回去用别的 A 的
       token —— 那样只会换来一次 4401 和一句看不懂的报错。
    2. 没给：用 `current` 指向的那个（最近一次成功的）。
    3. `current` 丢了但只有一个槽：用它。
    4. 旧布局的单文件：还能读到就先用着（升级友好，下次 save 时迁走）。

    有多个槽又没有 `current` 时返回 None，由调用方要求显式指定，而不是替用户挑
    一台 —— 猜错的话，那台 A 上会凭空多出一台同名节点。
    """
    explicit = (config.server or "").strip()
    if explicit:
        stored = _read(slot_path(config, explicit))
        if stored is not None:
            return stored
        legacy = _read(legacy_path(config))
        if legacy is not None and same_server(legacy.server, explicit):
            return legacy
        return None

    pointer = _current_server(config)
    if pointer:
        stored = _read(slot_path(config, pointer))
        if stored is not None:
            return stored
        logger.warning("current 指向 %s，但对应的凭据文件不见了", pointer)

    slots = [c for c in (_read(path) for path in _slot_files(config)) if c is not None]
    if len(slots) == 1:
        return slots[0]

    return _read(legacy_path(config))


def save(config: AgentConfig, credentials: Credentials) -> Path:
    """写进这台 A 自己的槽，并把 `current` 指过去。"""
    server = (credentials.server or "").strip()
    if not server:
        raise ValueError("凭据里没有 server，不知道该存进哪个槽")

    path = slot_path(config, server)
    payload = json.dumps(asdict(credentials), ensure_ascii=False, indent=2) + "\n"
    _write_secret(path, payload)

    # current 只是个指针，写失败不影响这次连接，下次带上 --server 就行
    try:
        _write_secret(current_path(config), canonical_server(server) + "\n")
    except OSError as exc:  # pragma: no cover - 磁盘满/只读挂载
        logger.warning("记不住 current 指针：%s", exc)

    _migrate_legacy(config)
    return path


def _migrate_legacy(config: AgentConfig) -> None:
    """旧布局的 agent.json 迁进它自己的槽，然后删掉。

    只迁不删的话，同一个 token 会在磁盘上留两份；直接删的话，万一解析不了就
    丢了唯一的凭据。所以解析成功才迁+删，解析失败就原地留着给人看。
    """
    path = legacy_path(config)
    legacy = _read(path)
    if legacy is None:
        return

    target = slot_path(config, legacy.server)
    if not target.exists():
        payload = json.dumps(asdict(legacy), ensure_ascii=False, indent=2) + "\n"
        _write_secret(target, payload)
        logger.info("旧凭据已迁移到 %s", target)
    _unlink(path)


def forget_slot(config: AgentConfig, server: str) -> None:
    """删掉某一台 A 的槽，不碰别的。

    和 `clear()` 的区别：clear 是"我要忘掉这一台"（会顺手收拾 current 和旧文件），
    这个是内部搬运用——同一个 token 不该在磁盘上留两份。
    """
    _unlink(slot_path(config, server))


def clear(config: AgentConfig) -> None:
    """忘掉一台 A（有 `--server` 就忘那台，否则忘 current 指向的那台）。"""
    explicit = (config.server or "").strip() or _current_server(config) or ""

    if explicit:
        _unlink(slot_path(config, explicit))
        if same_server(explicit, _current_server(config) or ""):
            _unlink(current_path(config))

    legacy = _read(legacy_path(config))
    if legacy is not None and (not explicit or same_server(legacy.server, explicit)):
        _unlink(legacy_path(config))
