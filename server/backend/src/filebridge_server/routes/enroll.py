"""设备注册端点：`/internal/enroll`。

这是 B 唯一一个**不需要 token**就能访问的入口，所以它的准入判据必须是显式的、
可审计的。三种策略（`FILEBRIDGE_ENROLL_POLICY`）：

- `first`（默认）：库里一台节点都没有时免审——空系统没有数据可被窃取，风险窗口
  只有"部署完到第一台 B 连上"这一小段。
- `open`：任何请求都自动登记。给完全可信的内网用，代价是任何人能连上 A 就能挂
  一台机器进来（占磁盘、占连接）。
- `closed`：全部转人工，`filebridge-server approve <id>` 放行。

**为什么不用 WebSocket 握手做注册**：那要改协议（两份 protocol.py）并处理版本
兼容，而这个动作一辈子只发生一次。用普通 HTTP 端点，协议层完全不动。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..deps import Context

logger = logging.getLogger("filebridge.enroll")

router = APIRouter(prefix="/internal/enroll", tags=["设备注册"])


class EnrollRequest(BaseModel):
    hostname: str | None = Field(default=None, max_length=128)
    os: str | None = Field(default=None, max_length=64)
    root_path: str | None = Field(default=None, max_length=4096)
    # 自助轮换：带上 agent_id + 旧 token，就不走审批
    agent_id: str | None = Field(default=None, max_length=64)
    old_token: str | None = Field(default=None, max_length=128)


@router.post("")
async def enroll(payload: EnrollRequest, request: Request, ctx: Context):
    client_ip = request.client.host if request.client else "unknown"
    if not ctx.limits.enroll.allow(client_ip or "unknown"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "注册请求过于频繁"},
        )

    # ① 轮换：凭旧 token 换新的，不需要审批
    if payload.agent_id and payload.old_token:
        row = await ctx.auth.authenticate_agent(payload.old_token)
        if row is None or row["id"] != payload.agent_id:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": "旧 token 无效或已吊销，无法自助轮换"},
            )
        token = await ctx.auth.rotate_agent_token(str(row["id"]))
        logger.info("agent %s rotated its token", row["id"])
        return {"status": "ok", "agent_id": str(row["id"]), "token": token}

    policy = ctx.settings.enroll_policy
    agent_count = int(await ctx.db.fetchval("SELECT COUNT(*) FROM agents", default=0) or 0)

    auto = policy == "open" or (policy == "first" and agent_count == 0)

    # ② 自动登记
    if auto:
        agent_id, token = await ctx.auth.create_agent(
            payload.hostname or "agent",
            hostname=payload.hostname,
            os_name=payload.os,
        )
        logger.info(
            "agent %s enrolled automatically (policy=%s) from %s",
            agent_id,
            policy,
            client_ip,
        )
        return {"status": "ok", "agent_id": agent_id, "token": token}

    # ③ 转人工
    pending_id = await ctx.auth.create_enroll_request(
        hostname=payload.hostname,
        os_name=payload.os,
        root_path=payload.root_path,
        remote_ip=client_ip,
    )
    logger.info(
        "enrollment from %s (%s) is pending: %s", client_ip, payload.hostname, pending_id
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "pending",
            "pending_id": pending_id,
            "hint": f"在 A 上执行：filebridge-server approve {pending_id}",
        },
    )


@router.get("/{pending_id}")
async def claim(pending_id: str, ctx: Context):
    """B 轮询用。只有 `approved` 才会现场签发 token，且只签一次。"""
    row = await ctx.auth.get_enroll_request(pending_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "file_not_found", "message": "没有这个注册请求"},
        )

    state = str(row["status"])
    if state == "pending":
        return {"status": "pending"}
    if state == "rejected":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "管理员已拒绝这次注册"},
        )
    if state == "claimed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "invalid_arguments", "message": "这次注册已经被领取过了"},
        )

    claimed = await ctx.auth.claim_enroll(pending_id)
    if claimed is None:  # 并发领取，慢的那一方
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "invalid_arguments", "message": "这次注册已经被领取过了"},
        )
    agent_id, token = claimed
    logger.info("agent %s claimed enrollment %s", agent_id, pending_id)
    return {"status": "ok", "agent_id": agent_id, "token": token}
