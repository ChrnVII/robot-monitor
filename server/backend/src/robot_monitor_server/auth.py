"""鉴权与授权。

两套身份，用两套机制，不要混：

- **C（浏览器）**：Argon2 密码 + 服务端 session 表 + HttpOnly Cookie。
  不用无状态 JWT，因为 JWT 在过期前无法吊销——登出、改密码之后旧 token
  仍然有效，这是 filebrowser 归档项目里被反复提到的问题。

- **B（agent）**：每台设备一个 token，库里只存 sha256。token 只在签发时
  明文显示一次，之后无法从库里还原。吊销是改一行的 `revoked_at`，立即生效。

token 以 sha256 形式入库，校验就是"算哈希 → 按索引查"，库里没有明文可还原。
用户名比较走 `hmac.compare_digest`：`==` 会在第一个不同字节处提前返回，时序差
足以被逐字符还原。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .config import Settings
from .db import Database

_hasher = PasswordHasher()

# agent token 前缀，便于在日志/配置里一眼认出这是哪种凭据
AGENT_TOKEN_PREFIX = "rma_"


# --- 密码 ----------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password_async(password: str, stored_hash: str) -> bool:
    """在工作线程里校验密码。

    Argon2 是刻意的慢哈希：默认参数一次约 40ms 纯 CPU、64MB 内存。直接在协程里
    调用等于让整个事件循环停 40ms——这期间 WS 心跳发不出去、SSE 不推、分片 PUT
    不落盘。单用户场景下登录很稀疏，所以这不是瓶颈，但"一次登录卡住所有在传的
    文件"是个不该有的耦合。
    """
    return await asyncio.to_thread(verify_password, password, stored_hash)


# --- token ---------------------------------------------------------------


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(raw: str) -> str:
    """token 的入库形态。sha256 足以防止库泄露后被直接使用。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_agent_token() -> str:
    return AGENT_TOKEN_PREFIX + new_token(32)


# --- 会话 ----------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    id: str
    expires_at: float


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # --- C 侧 -----------------------------------------------------------

    async def _credentials(self) -> tuple[str, str] | None:
        """当前生效的 (用户名, 密码哈希)。密码只有数据库这一份真相。"""
        row = await self.db.fetchone("SELECT username, password_hash FROM admin_user WHERE id = 1")
        if row is None:
            return None
        return str(row["username"]), str(row["password_hash"])

    async def password_configured(self) -> bool:
        return await self._credentials() is not None

    async def set_password(self, username: str, password: str) -> None:
        await self.db.execute(
            "INSERT INTO admin_user (id, username, password_hash, updated_at) VALUES (1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET username = excluded.username,"
            " password_hash = excluded.password_hash, updated_at = excluded.updated_at",
            (username, hash_password(password), time.time()),
        )

    async def change_password(self, current: str, new: str) -> bool:
        """改密码，并让所有已登录会话立即失效。

        不失效的话，改密码对已经登进去的人没有任何作用——那等于没改。
        """
        credentials = await self._credentials()
        if credentials is None:
            return False
        username, stored = credentials
        if not await verify_password_async(current, stored):
            return False

        await self.set_password(username, new)
        await self.db.execute("DELETE FROM sessions")
        await self.audit("admin", "auth.change_password", target=username)
        return True

    async def login(self, username: str, password: str) -> str | None:
        """成功返回明文 session token（写进 Cookie），失败返回 None。

        用户名比较也用 compare_digest：单用户场景下泄露"用户名对不对"
        没有实际价值，但不写平等的代码迟早会被复制到别处。
        """
        credentials = await self._credentials()
        if credentials is None:
            return None
        expected_name, stored_hash = credentials
        if not hmac.compare_digest(username.strip(), expected_name):
            return None
        if not await verify_password_async(password, stored_hash):
            return None

        raw = new_token(32)
        now = time.time()
        expires = now + self.settings.session_ttl_hours * 3600
        await self.db.execute(
            "INSERT INTO sessions (id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (uuid.uuid4().hex, hash_token(raw), now, expires),
        )
        await self.audit("admin", "auth.login", target=expected_name)
        # 初始密码落盘文件只在"还没有人登录过"时有意义，登录成功即删
        self.clear_initial_password_file()
        return raw

    def clear_initial_password_file(self) -> None:
        """删掉自动生成的初始密码文件。失败无所谓——它是 best effort。"""
        path = self.settings.data_dir / "initial-password"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    async def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        await self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(raw_token),))
        await self.audit("admin", "auth.logout")

    async def verify_session(self, raw_token: str | None) -> Session | None:
        if not raw_token:
            return None
        row = await self.db.fetchone(
            "SELECT id, expires_at FROM sessions WHERE token_hash = ?", (hash_token(raw_token),)
        )
        if row is None or row["expires_at"] <= time.time():
            return None
        return Session(id=row["id"], expires_at=row["expires_at"])

    async def purge_expired_sessions(self) -> int:
        cur = await self.db.fetchval(
            "SELECT COUNT(*) FROM sessions WHERE expires_at <= ?", (time.time(),), default=0
        )
        if cur:
            await self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
        return int(cur)

    # --- B 侧 -----------------------------------------------------------

    async def issue_agent_token(
        self,
        name: str,
        *,
        hostname: str | None = None,
        os_name: str | None = None,
    ) -> tuple[str, str]:
        """签发（或轮换）一台 agent 的 token。返回 (agent_id, 明文 token)。"""
        raw = new_agent_token()
        now = time.time()
        existing = await self.db.fetchone("SELECT id FROM agents WHERE name = ?", (name,))
        if existing:
            agent_id = existing["id"]
            await self.db.execute(
                "UPDATE agents SET token_hash = ?, revoked_at = NULL, hostname = COALESCE(?, hostname),"
                " os = COALESCE(?, os) WHERE id = ?",
                (hash_token(raw), hostname, os_name, agent_id),
            )
        else:
            agent_id = uuid.uuid4().hex[:12]
            await self.db.execute(
                "INSERT INTO agents (id, name, token_hash, hostname, os, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, name, hash_token(raw), hostname, os_name, now),
            )
        await self.audit("admin", "agent.issue_token", target=agent_id, detail=name)
        return agent_id, raw

    async def create_agent(
        self,
        name: str,
        *,
        hostname: str | None = None,
        os_name: str | None = None,
    ) -> tuple[str, str]:
        """新建一台 agent。名字冲突时自动加后缀，**不会**覆盖已有节点。

        和 issue_agent_token 的区别很重要：那个是管理员按名字指定，重名就该轮换；
        这个是设备自报的 hostname，重名只说明两台机器同名（或者有人在冒充），
        绝不能因此把真实节点的 token 换掉。
        """
        raw = new_agent_token()
        now = time.time()

        candidate = name
        for suffix in range(2, 100):
            existing = await self.db.fetchone("SELECT id FROM agents WHERE name = ?", (candidate,))
            if existing is None:
                break
            candidate = f"{name}-{suffix}"
        else:  # pragma: no cover - 同名的机器不会有一百台
            candidate = f"{name}-{uuid.uuid4().hex[:4]}"

        agent_id = uuid.uuid4().hex[:12]
        await self.db.execute(
            "INSERT INTO agents (id, name, token_hash, hostname, os, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, candidate, hash_token(raw), hostname, os_name, now),
        )
        await self.audit("agent", "agent.enroll", target=agent_id, detail=candidate)
        return agent_id, raw

    async def rotate_agent_token(self, agent_id: str) -> str | None:
        """给已知设备换一个 token（凭旧 token 自助完成）。"""
        row = await self.db.fetchone("SELECT id FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            return None
        raw = new_agent_token()
        await self.db.execute(
            "UPDATE agents SET token_hash = ?, revoked_at = NULL WHERE id = ?",
            (hash_token(raw), agent_id),
        )
        await self.audit("agent", "agent.rotate", target=agent_id)
        return raw

    # --- 设备注册 -------------------------------------------------------

    async def create_enroll_request(
        self,
        *,
        hostname: str | None,
        os_name: str | None,
        root_path: str | None,
        remote_ip: str | None,
    ) -> str:
        pending_id = uuid.uuid4().hex
        now = time.time()
        await self.db.execute(
            "INSERT INTO enroll_requests (id, hostname, os, root_path, remote_ip, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (pending_id, hostname, os_name, root_path, remote_ip, now, now),
        )
        await self.audit("agent", "enroll.request", target=pending_id, detail=hostname)
        return pending_id

    async def get_enroll_request(self, pending_id: str) -> aiosqlite.Row | None:
        return await self.db.fetchone("SELECT * FROM enroll_requests WHERE id = ?", (pending_id,))

    async def list_enroll_requests(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            rows = await self.db.fetchall(
                "SELECT * FROM enroll_requests WHERE status = ? ORDER BY created_at", (status,)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM enroll_requests ORDER BY created_at DESC")
        return [dict(row) for row in rows]

    async def set_enroll_status(self, pending_id: str, status: str) -> bool:
        if status not in {"approved", "rejected"}:
            raise ValueError(f"unexpected enroll status {status!r}")
        row = await self.get_enroll_request(pending_id)
        if row is None or row["status"] != "pending":
            return False
        await self.db.execute(
            "UPDATE enroll_requests SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), pending_id),
        )
        await self.audit("admin", f"enroll.{status}", target=pending_id, detail=row["hostname"])
        return True

    async def claim_enroll(self, pending_id: str) -> tuple[str, str] | None:
        """已批准的请求被 B 领走：现场签发 token，一次性。

        不把明文 token 存进 enroll_requests —— 那张表是"待办清单"，
        不该成为凭据的第二个存放点。
        """
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM enroll_requests WHERE id = ?", (pending_id,)
            )
            row = await cursor.fetchone()
            if row is None or row["status"] != "approved":
                return None
            # 先占位再签发，避免两个并发请求各签一个 token
            await conn.execute(
                "UPDATE enroll_requests SET status = 'claimed', updated_at = ? WHERE id = ?",
                (time.time(), pending_id),
            )

        agent_id, token = await self.create_agent(
            row["hostname"] or "agent",
            hostname=row["hostname"],
            os_name=row["os"],
        )
        await self.db.execute(
            "UPDATE enroll_requests SET agent_id = ? WHERE id = ?", (agent_id, pending_id)
        )
        return agent_id, token

    async def authenticate_agent(self, raw_token: str | None) -> aiosqlite.Row | None:
        """按 token 找 agent。吊销、空 token、格式不对一律返回 None。"""
        if not raw_token or not raw_token.startswith(AGENT_TOKEN_PREFIX):
            return None
        row = await self.db.fetchone("SELECT * FROM agents WHERE token_hash = ?", (hash_token(raw_token),))
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        return row

    async def find_agent(self, agent_id: str) -> aiosqlite.Row | None:
        return await self.db.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))

    async def explain_rejected_token(self, agent_id: str, old_token: str) -> str:
        """轮换被拒时，说清楚是哪一种拒。

        三种情况对设备的处置完全不同，压成一句"无效"就只能让人去删凭据：

        - `unknown_agent`：这台 A 没有这个 id。要么是另一台 A（换了服务器），
          要么节点被删了。设备应该重新注册——会进待批准列表。
        - `revoked`：id 在、被管理员吊销了。设备**不该**自动重新注册，那等于把
          管理员踢掉的机器又放回来。
        - `token_mismatch`：id 在、没被吊销，但 token 对不上（被 issue-token 重签
          过，或库是从别处恢复的）。只能人工处理，设备自己怎么试都没用。

        哈希比较用 compare_digest：这个端点谁都能打，不给它留逐字节的计时旁路。
        """
        row = await self.find_agent(agent_id)
        if row is None:
            return "unknown_agent"
        if row["revoked_at"] is not None:
            return "revoked"
        if not hmac.compare_digest(str(row["token_hash"]), hash_token(old_token)):
            return "token_mismatch"
        # token 其实是对的却没被 authenticate_agent 认出来，只可能是调用方给的
        # agent_id 与 token 所属的不是同一台。按"不认识"处理，不额外透露信息。
        return "unknown_agent"

    async def revoke_agent(self, agent_id: str) -> bool:
        row = await self.db.fetchone("SELECT id FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            return False
        await self.db.execute(
            "UPDATE agents SET revoked_at = ?, online = 0 WHERE id = ?", (time.time(), agent_id)
        )
        await self.audit("admin", "agent.revoke", target=agent_id)
        return True

    async def mark_agent_online(self, agent_id: str, *, online: bool) -> None:
        await self.db.execute(
            "UPDATE agents SET online = ?, last_seen = ? WHERE id = ?",
            (1 if online else 0, time.time(), agent_id),
        )

    async def touch_agent(self, agent_id: str) -> None:
        await self.db.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (time.time(), agent_id))

    # --- 审计 -----------------------------------------------------------

    async def audit(
        self, actor: str, action: str, *, target: str | None = None, detail: str | None = None
    ) -> None:
        await self.db.execute(
            "INSERT INTO audit_log (ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), actor, action, target, detail),
        )

    async def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT ts, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
