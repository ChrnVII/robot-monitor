"""设备注册：`POST /internal/enroll`。

这一步只在"本地没有可用凭据"时发生，之后 token 落在 `credentials.py` 管理的文件里。

三种返回：

- `200 {token}` —— 直接放行（A 侧策略是 first 且库里没有节点，或 open）
- `202 {pending_id}` —— 转人工，B 在这里等，直到管理员 approve
- `401/403/429` —— 明确报错，不做无限重试

等待期间每 3 秒问一次，最多 10 分钟。等待是"看得见的"：终端上会打印当前状态和
需要在 A 上执行的命令，而不是静默卡住。

**地址变了怎么办：** 凭据按地址分槽存（见 credentials.py），所以 A 换域名/IP
之后，本地看起来"没有这台 A 的凭据"。这时先去 `GET /api/v1/health` 问一句
`instance_id`——它是 A 的身份，与地址无关。对上了就沿用原 token，只把凭据挪到
新地址；对不上才当新机器注册。

刻意**不**用"把旧 token 发给新地址试一下"这种判据：那等于把一份长期口令交给一个
还没确认是谁的地址。`/api/v1/health` 是公开的，问这一句不交出任何凭据。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass

import httpx

from .config import AgentConfig, ConfigError
from .credentials import (
    Credentials,
    forget_slot,
    known_servers,
    load,
    same_server,
    save,
    stored_slots,
)

logger = logging.getLogger("robot-monitor.agent.enroll")

POLL_INTERVAL = 3.0
POLL_TIMEOUT = 600.0
REQUEST_TIMEOUT = 15.0
# 问 instance_id 是"顺便问一句"，不该比注册本身还慢
HEALTH_TIMEOUT = 5.0


class EnrollError(Exception):
    """注册失败，调用方应把消息原样打给用户。"""


@dataclass(frozen=True)
class Rotation:
    """自助轮换的结果。失败也带原因，好让调用方说一句有用的话。"""

    token: str | None = None
    code: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.token is not None


def _client(config: AgentConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.http_base,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        verify=config.verify_tls,
    )


async def server_instance_id(config: AgentConfig) -> str | None:
    """问一句"你是哪台 A"。公开端点，不带任何凭据。

    拿不到（旧版 A 没有这个字段、网络不通、返回不是 JSON）就返回 None，
    调用方按"认不出来"处理，退回到原来的注册流程——不能因为这一句失败就卡死。
    """
    try:
        # 复用 _client：它是测试里唯一的注入点（httpx.MockTransport 挂在上面）。
        # 单独再建一个 client 的话，这个函数就永远测不到。
        async with _client(config) as client:
            response = await client.get("/api/v1/health", timeout=HEALTH_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.debug("问不到 %s 的身份：%s", config.http_base, exc)
        return None

    if response.status_code != 200:
        return None

    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("instance_id") if isinstance(body, dict) else None
    return value if isinstance(value, str) and value else None


async def adopt_same_server(config: AgentConfig) -> Credentials | None:
    """地址变了，但还是同一台 A？认出它，别把自己当成新机器。

    条件只有一个：新地址报出的 `instance_id` 与本地某个槽记的一致。地址变了它
    不会变，所以这是"同一台"最直接的证据。
    """
    candidates = [item for item in stored_slots(config) if item.instance_id]
    if not candidates:
        return None

    instance = await server_instance_id(config)
    if not instance:
        return None

    for candidate in candidates:
        if candidate.instance_id != instance:
            continue

        credentials = Credentials(
            server=config.server,
            token=candidate.token,
            agent_id=candidate.agent_id,
            root=candidate.root,
            name=candidate.name,
            instance_id=instance,
        )
        # 先写新地址的槽（顺带把 current 指过去），再删旧的：同一个 token 不该
        # 在磁盘上留两份，但也不能在删完之后才发现新的一份没写成功。
        save(config, credentials)
        if not same_server(candidate.server, config.server):
            # 两种写法落到同一个槽时（理论上进不到这里）绝不删——那是刚写的那份。
            forget_slot(config, candidate.server)

        print(
            f"✓ 认出同一台 A（instance {instance[:8]}）：地址由 {candidate.server} "
            f"变为 {config.server}，沿用原凭据，不重新注册"
        )
        return credentials

    return None


async def ensure_credentials(config: AgentConfig) -> AgentConfig:
    """把 server / token / root 补齐，必要时向 A 注册。返回同一个 config（已填充）。"""
    stored = load(config)

    # 这个地址本地没有凭据时，先问一句"你是哪台 A"。换了域名/IP 但还是同一台，
    # 就沿用原 token，别把自己当新机器注册出一台重名节点来。
    if stored is None and config.server.strip():
        stored = await adopt_same_server(config)

    if stored is not None:
        # 命令行与环境变量优先，本地凭据只补空缺
        config.server = config.server or stored.server
        config.token = config.token or stored.token
        config.agent_id = config.agent_id or stored.agent_id
        config.instance_id = config.instance_id or stored.instance_id
        config.root = config.root or (stored.root or "")
        config.name = config.name or stored.name

    if not config.server:
        known = known_servers(config)
        if known:
            raise ConfigError(
                "这台机器记着好几台 A 的凭据，用 --server 指定这次连哪一台：\n  "
                + "\n  ".join(known)
            )
        raise ConfigError("缺少 A 的地址：用 --server http://10.0.0.5:8000 指定（只需第一次）")

    if config.token:
        if not config.root:
            raise ConfigError("缺少 --root：指定要共享的目录")
        return config

    # 换过 A 的时候这句要说得准：不是"这台机器没有凭据"，是"这台 A 没见过我"。
    # 别的 A 的凭据原样留着，以后换回去还是老身份。
    others = [s for s in known_servers(config) if not same_server(s, config.server)]
    if others:
        print(f"本地没有 {config.server} 的凭据（另有 {len(others)} 台 A 的凭据保留着），开始注册…")
    else:
        print(f"本地没有凭据，向 {config.server} 注册…")
    credentials = await enroll(config)
    config.token = credentials.token
    config.agent_id = credentials.agent_id
    # 别漏了这一行：instance_id 只写在文件里、不回填 config 的话，下一次轮换
    # token 时 rotate() 会拿着 None 把它覆盖掉，这台 A 从此就认不出来了。
    config.instance_id = config.instance_id or credentials.instance_id
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
            return await _persist(config, body)

        if response.status_code == 202:
            body = response.json()
            pending_id = body.get("pending_id")
            if not pending_id:
                raise EnrollError("A 返回了待批准，但没有给 pending_id")
            print(f"⏳ 已提交，等待 A 批准（{body.get('hint') or '需要在 A 上批准'}）")
            return await _wait_for_approval(client, config, pending_id)

        raise EnrollError(f"注册失败（HTTP {response.status_code}）：{_message(response)}")


async def rotate(config: AgentConfig) -> Rotation:
    """凭旧 token 换一个新的。

    失败时把 A 给的原因带回去（`unknown_agent` / `revoked` / `token_mismatch`）：
    这三种的处置完全不同，压成一句"无效"就只能让人去删文件（见 A 侧
    `auth.explain_rejected_token`）。
    """
    if not config.token or not config.agent_id:
        return Rotation(code="no_credentials")

    async with _client(config) as client:
        try:
            response = await client.post(
                "/internal/enroll",
                json={"agent_id": config.agent_id, "old_token": config.token},
            )
        except httpx.HTTPError as exc:
            logger.warning("轮换 token 失败：%s", exc)
            return Rotation(code="unreachable", message=str(exc))

        if response.status_code != 200:
            code, message = _rejection(response)
            logger.warning(
                "轮换 token 被拒绝（HTTP %s, %s）：%s", response.status_code, code, message
            )
            return Rotation(code=code, message=message)

        token = response.json().get("token")
        if not isinstance(token, str) or not token:
            return Rotation(code="unexpected_response")

        save(
            config,
            Credentials(
                server=config.server,
                token=token,
                agent_id=config.agent_id,
                root=config.root or None,
                name=config.name,
                instance_id=config.instance_id,
            ),
        )
        return Rotation(token=token)


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
                    print(f"   仍在等待批准：到 A 的「节点」页点批准（或 robot-monitor-server approve {pending_id[:10]}）")
                continue
            return await _persist(config, body)

        if response.status_code == 403:
            raise EnrollError("管理员拒绝了这次注册")
        if response.status_code == 409:
            raise EnrollError("这次注册已经被领取过了（可能是另一个进程先完成了注册）")
        if response.status_code == 404:
            raise EnrollError("A 上找不到这条注册申请（可能已被清理）")

        logger.warning("查询注册状态返回 HTTP %s：%s", response.status_code, _message(response))

    raise EnrollError(
        f"等待批准超过 {int(POLL_TIMEOUT)}s。在 A 上执行 "
        f"`robot-monitor-server pending` 看看有没有这条申请，或重新发起注册。"
    )


async def _persist(config: AgentConfig, body: dict) -> Credentials:
    token = body.get("token")
    agent_id = body.get("agent_id")
    if not isinstance(token, str) or not isinstance(agent_id, str):
        raise EnrollError("A 的响应里没有 token / agent_id")

    # 顺手记住这台 A 的 instance_id：下次它换地址时，就靠这个认出"还是它"。
    # 问不到就算了——凭据本身是好的，不该因为这个让注册失败。
    instance_id = await server_instance_id(config) or config.instance_id

    credentials = Credentials(
        server=config.server,
        token=token,
        agent_id=agent_id,
        root=config.root or None,
        name=config.name,
        instance_id=instance_id,
    )
    path = save(config, credentials)
    print(f"✓ 已登记 agent_id={agent_id}，凭据写入 {path}（0600）")
    return credentials


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    try:
        return await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise EnrollError(f"连不上 {client.base_url}：{exc}") from exc


def _rejection(response: httpx.Response) -> tuple[str | None, str | None]:
    """从 401 里取出 (code, message)。A 现在会把原因分开给（见 routes/enroll.py）。"""
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:200]
    if not isinstance(body, dict):
        return None, str(body)[:200]

    detail = body.get("detail")
    if isinstance(detail, dict):
        code = detail.get("code")
        return (code if isinstance(code, str) else None), str(detail.get("message") or "")
    return None, str(body.get("message") or body)[:200]


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or body)
    return str(body)[:200]
