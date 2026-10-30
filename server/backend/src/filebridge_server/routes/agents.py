"""节点列表、目录浏览、待批准设备、删除节点。

目录列表是**透传**：A 不缓存、不合并、不解释。B 是唯一知道目录内容的一方，
A 多存一份就多一份"两边不一致"的可能。10s 超时是硬性的——前端展开一个节点
转十秒还不如直接报错让它重试。

待批准设备的三个接口（列表 / 批准 / 拒绝）是 CLI 那三个命令的等价物，只是
让浏览器能直接处理。它们走会话鉴权，和 `/internal/enroll` 那条 B 用的通道
完全隔离：B 只能"提交申请"，不能自己批准自己。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import Context, require_session
from ..errors import from_agent_error
from ..hub import AgentError
from ..paths import normalize_client_path
from ..protocol import CommandName, ErrorCode

router = APIRouter(
    prefix="/api/v1/agents",
    dependencies=[Depends(require_session)],
    tags=["节点"],
)


class ListDirPayload(BaseModel):
    path: str = Field(default="", max_length=4096)
    cursor: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=200, ge=1, le=1000)


@router.get("")
async def list_agents(ctx: Context) -> dict[str, Any]:
    rows = await ctx.db.fetchall("SELECT * FROM agents ORDER BY created_at")
    live = ctx.hub.snapshot()
    counts = {
        str(row["agent_id"]): int(row["n"])
        for row in await ctx.db.fetchall(
            "SELECT agent_id, COUNT(*) AS n FROM files"
            " WHERE deleted_at IS NULL GROUP BY agent_id"
        )
    }
    items = []
    for row in rows:
        runtime = live.get(row["id"])
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "online": runtime is not None,
                "hostname": (runtime or {}).get("hostname") or row["hostname"],
                "os": (runtime or {}).get("os") or row["os"],
                "agent_version": (runtime or {}).get("agent_version") or row["agent_version"],
                "roots": _parse_roots(row["roots"]),
                "last_seen": (runtime or {}).get("last_seen") or row["last_seen"],
                "connected_at": (runtime or {}).get("connected_at"),
                "revoked": row["revoked_at"] is not None,
                "files": counts.get(str(row["id"]), 0),
            }
        )
    return {"items": items, "total": len(items)}


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, ctx: Context, delete_files: bool = False) -> dict[str, Any]:
    """注销一台节点。

    `delete_files=true` 才会动文件，而且只把它们移进回收站——删节点是个
    不可逆动作，不该顺手把唯一的副本也删了。
    """
    return await ctx.tasks.remove_agent(agent_id, delete_files=delete_files)


@router.get("/pending")
async def list_pending(ctx: Context) -> dict[str, Any]:
    rows = await ctx.auth.list_enroll_requests("pending")
    items = [
        {
            "id": row["id"],
            "hostname": row["hostname"],
            "os": row["os"],
            "root_path": row["root_path"],
            "remote_ip": row["remote_ip"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/pending/{pending_id}/approve")
async def approve_pending(pending_id: str, ctx: Context) -> dict[str, Any]:
    return await _resolve_pending(ctx, pending_id, "approved")


@router.post("/pending/{pending_id}/reject")
async def reject_pending(pending_id: str, ctx: Context) -> dict[str, Any]:
    return await _resolve_pending(ctx, pending_id, "rejected")


async def _resolve_pending(ctx: Context, pending_id: str, outcome: str) -> dict[str, Any]:
    """批准或拒绝一次注册申请。

    只改状态、不签发 token：token 由 B 下次轮询 `/internal/enroll/{id}` 时
    现场领取，一次性。所以"批准"这个动作本身不产生任何凭据副本。
    """
    row = await ctx.auth.get_enroll_request(pending_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": ErrorCode.FILE_NOT_FOUND.value, "message": "没有这个注册申请"},
        )
    if row["status"] != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": ErrorCode.INVALID_ARGUMENTS.value,
                "message": f"这条申请已经处理过了（{row['status']}）",
            },
        )
    if not await ctx.auth.set_enroll_status(pending_id, outcome):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": ErrorCode.INVALID_ARGUMENTS.value, "message": "申请状态已变化，请刷新"},
        )

    hostname = row["hostname"] or pending_id[:10]
    ctx.bus.publish(
        "enroll_resolved",
        {"pending_id": pending_id, "status": outcome, "hostname": hostname},
    )
    return {"ok": True, "status": outcome, "hostname": hostname, "pending_id": pending_id}


@router.post("/{agent_id}/list")
async def list_dir(agent_id: str, payload: ListDirPayload, ctx: Context) -> dict[str, Any]:
    try:
        rel = normalize_client_path(payload.path)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": ErrorCode.INVALID_ARGUMENTS.value, "message": str(exc)},
        ) from exc

    if not ctx.hub.is_online(agent_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": ErrorCode.AGENT_OFFLINE.value, "message": "节点不在线"},
        )

    started = time.perf_counter()
    try:
        result = await ctx.hub.request(
            agent_id,
            CommandName.LIST_DIR,
            {"rel": rel, "cursor": payload.cursor, "limit": payload.limit},
            timeout=ctx.settings.command_timeout_seconds,
        )
    except AgentError as exc:
        error = from_agent_error(exc)
        raise HTTPException(
            error.status_code, detail={"code": exc.code.value, "message": exc.message}
        ) from exc

    result.setdefault("path", rel)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def _parse_roots(raw: Any) -> list[str]:
    import json

    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []
