"""设备注册：`POST /internal/enroll`。

这一步只在"本地没有可用凭据"时发生，之后 token 落在 `credentials.py` 管理的文件里。

三种返回：

- `200 {token}` —— 直接放行（A 侧策略是 first 且库里没有节点，或 open）
- `202 {pending_id}` —— 转人工，B 在这里等，直到管理员 approve
- `401/403/429` —— 明确报错，不做无限重试

等待期间每 3 秒问一次，最多 10 分钟。等待是"看得见的"：终端上会打印当前状态和
需要在 A 上执行的命令，而不是静默卡住。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import httpx

from .config import AgentConfig, ConfigError
from .credentials import Credentials, load, save

logger = logging.getLogger("filebridge.agent.enroll")

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 600.0
REQUEST_TIMEOUT = 15.0


class EnrollError(Exception):
    """注册失败，调用方应把消息原样打给用户。"""


def _client(config: AgentConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.http_base,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        verify=config.verify_tls,
    )


async def ensure_credentials(config: AgentConfig) -> AgentConfig:
    """把 server / token / root 补齐，必要时向 A 注册。返回同一个 config（已填充）。"""
    stored = load(config)
    if stored is not None:
        # 命令行与环境变量优先，本地凭据只补空缺
        config.server = config.server or stored.server
        config.token = config.token or stored.token
        config.agent_id = config.agent_id or stored.agent_id
        config.root = config.root or (stored.root or "")
        config.name = config.name or stored.name

    if not config.server:
        raise ConfigError("缺少 A 的地址：用 --server http://10.0.0.5:8000 指定（只需第一次）")

    if config.token:
        if not config.root:
            raise ConfigError("缺少 --root：指定要共享的目录")
        return config

    print(f"本地没有凭据，向 {config.server} 注册…")
    credentials = await enroll(config)
    config.token = credentials.token
    config.agent_id = credentials.agent_id
    config.root = config.root or (credentials.root or "")
    if not config.root:
        raise ConfigError("缺少 --root：指定要共享的目录")
    return config


async def enroll(config: AgentConfig) -> Credentials:
    payload = {
        "hostname": config.name or socket.gethostname(),
        "os": "linux",
        "root_path": config.root or None,
    }

    async with _client(config) as client:
        response = await _post(client, "/internal/enroll", payload)

        if response.status_code == 200:
            body = response.json()
            return _persist(config, body)

        if response.status_code == 202:
            body = response.json()
            pending_id = body.get("pending_id")
            if not pending_id:
                raise EnrollError("A 返回了待批准，但没有给 pending_id")
            print(f"⏳ 已提交，等待 A 批准（{body.get('hint') or '需要在 A 上批准'}）")
            return await _wait_for_approval(client, config, pending_id)

        raise EnrollError(f"注册失败（HTTP {response.status_code}）：{_message(response)}")


async def rotate(config: AgentConfig) -> str | None:
    """凭旧 token 换一个新的。成功返回新 token。"""
    if not config.token or not config.agent_id:
        return None

    async with _client(config) as client:
        try:
            response = await client.post(
                "/internal/enroll",
                json={"agent_id": config.agent_id, "old_token": config.token},
            )
        except httpx.HTTPError as exc:
            logger.warning("轮换 token 失败：%s", exc)
            return None

        if response.status_code != 200:
            logger.warning("轮换 token 被拒绝（HTTP %s）：%s", response.status_code, _message(response))
            return None

        token = response.json().get("token")
        if not isinstance(token, str) or not token:
            return None

        save(
            config,
            Credentials(
                server=config.server,
                token=token,
                agent_id=config.agent_id,
                root=config.root or None,
                name=config.name,
            ),
        )
        return token


async def _wait_for_approval(
    client: httpx.AsyncClient, config: AgentConfig, pending_id: str
) -> Credentials:
    deadline = time.monotonic() + POLL_TIMEOUT
    last_note = 0.0

    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            response = await client.get(f"/internal/enroll/{pending_id}")
        except httpx.HTTPError as exc:
            logger.debug("查询注册状态失败，稍后重试：%s", exc)
            continue

        if response.status_code == 200:
            body = response.json()
            if body.get("status") == "pending":
                if time.monotonic() - last_note > 30:
                    last_note = time.monotonic()
                    print(f"   仍在等待批准（filebridge-server approve {pending_id[:10]}）")
                continue
            return _persist(config, body)

        if response.status_code == 403:
            raise EnrollError("管理员拒绝了这次注册")
        if response.status_code == 409:
            raise EnrollError("这次注册已经被领取过了（可能是另一个进程先完成了注册）")
        if response.status_code == 404:
            raise EnrollError("A 上找不到这条注册申请（可能已被清理）")

        logger.warning("查询注册状态返回 HTTP %s：%s", response.status_code, _message(response))

    raise EnrollError(
        f"等待批准超过 {int(POLL_TIMEOUT)}s。在 A 上执行 "
        f"`filebridge-server pending` 看看有没有这条申请，或重新发起注册。"
    )


def _persist(config: AgentConfig, body: dict) -> Credentials:
    token = body.get("token")
    agent_id = body.get("agent_id")
    if not isinstance(token, str) or not isinstance(agent_id, str):
        raise EnrollError("A 的响应里没有 token / agent_id")

    credentials = Credentials(
        server=config.server,
        token=token,
        agent_id=agent_id,
        root=config.root or None,
        name=config.name,
    )
    path = save(config, credentials)
    print(f"✓ 已登记 agent_id={agent_id}，凭据写入 {path}（0600）")
    return credentials


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    try:
        return await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise EnrollError(f"连不上 {client.base_url}：{exc}") from exc


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or body)
    return str(body)[:200]
