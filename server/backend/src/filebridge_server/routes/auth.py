"""登录 / 登出 / 当前会话。

Cookie 的三个属性都不是可选的：

- `HttpOnly`：JS 读不到，XSS 拿不走 token
- `SameSite=Lax`：跨站 POST 不带 cookie，挡掉大部分 CSRF
- `Secure`：上 HTTPS 后必须开，http 下会被浏览器丢弃所以本地默认关

为什么用 Cookie 而不是 Authorization header：SSE 的 `EventSource` 无法携带
自定义 header，要让它能鉴权只剩 Cookie 一条路。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..deps import Context, SessionToken
from ..protocol import ErrorCode

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


class LoginPayload(BaseModel):
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=1024)


class LoginResponse(BaseModel):
    username: str
    expires_at: float


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginPayload, request: Request, response: Response, ctx: Context
) -> LoginResponse:
    client = request.client.host if request.client else "unknown"
    if not ctx.limits.login.allow(client):
        retry = ctx.limits.login.retry_after(client)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.RATE_LIMITED.value, "message": "尝试过于频繁，请稍后再试"},
            headers={"Retry-After": str(retry)},
        )

    # 密码现在存在数据库里（env 只是升级兼容的回退），所以这里必须问服务层，
    # 不能再看 settings.auth_configured。
    if not await ctx.auth.password_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": ErrorCode.INTERNAL.value,
                "message": "服务端还没有设置管理员密码，请先运行 filebridge-server passwd",
            },
        )

    token = await ctx.auth.login(payload.username, payload.password)
    if token is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"code": ErrorCode.UNAUTHORIZED.value, "message": "用户名或密码不正确"},
        )

    ttl = ctx.settings.session_ttl_hours * 3600
    response.set_cookie(
        key=ctx.settings.cookie_name,
        value=token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=ctx.settings.cookie_secure,
        path="/",
    )
    return LoginResponse(username=ctx.settings.admin_username, expires_at=time.time() + ttl)


@router.post("/logout")
async def logout(response: Response, ctx: Context, token: SessionToken) -> dict[str, bool]:
    await ctx.auth.logout(token)
    response.delete_cookie(ctx.settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
async def me(ctx: Context, token: SessionToken) -> dict[str, object]:
    session = await ctx.auth.verify_session(token)
    return {
        "username": ctx.settings.admin_username,
        "expires_at": session.expires_at if session else None,
    }
