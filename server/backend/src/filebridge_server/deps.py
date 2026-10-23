"""请求级依赖：从 app.state 取服务，并做会话校验。

会话校验放在依赖里而不是中间件：中间件对 WebSocket 和 SSE 的处理容易漏，
而依赖能精确地只保护需要保护的路由。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .auth import AuthService
from .config import Settings
from .db import Database
from .events import EventBus
from .hub import Hub
from .protocol import ErrorCode
from .ratelimit import SlidingWindow
from .services import TaskService
from .storage import Storage


@dataclass
class Limits:
    login: SlidingWindow
    task: SlidingWindow
    enroll: SlidingWindow


@dataclass
class AppContext:
    settings: Settings
    db: Database
    auth: AuthService
    hub: Hub
    storage: Storage
    bus: EventBus
    tasks: TaskService
    limits: Limits


def context(request: Request) -> AppContext:
    return request.app.state.ctx


Context = Annotated[AppContext, Depends(context)]


async def require_session(request: Request) -> str:
    """校验浏览器会话，返回原始 token。

    失败一律 401 + `unauthorized`。不区分"没带 cookie"和"cookie 过期"：
    对攻击者来说这两个信息没有区别，对前端来说都该跳登录页。
    """
    ctx: AppContext = request.app.state.ctx
    raw = request.cookies.get(ctx.settings.cookie_name)
    session = await ctx.auth.verify_session(raw)
    if session is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": ErrorCode.UNAUTHORIZED.value, "message": "未登录或会话已过期"},
        )
    return raw or ""


SessionToken = Annotated[str, Depends(require_session)]


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None


async def require_agent(request: Request) -> str:
    """校验 B 的设备 token，返回 agent_id。

    token 只从 Authorization header 取，不接受 `?token=`——query 会进
    Nginx 的 access log，等于把凭据写进日志文件。
    """
    ctx: AppContext = request.app.state.ctx
    row = await ctx.auth.authenticate_agent(bearer_token(request))
    if row is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": ErrorCode.UNAUTHORIZED.value, "message": "agent token 无效或已吊销"},
        )
    return str(row["id"])


AgentId = Annotated[str, Depends(require_agent)]
