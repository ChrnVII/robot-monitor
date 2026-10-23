"""节点列表与目录浏览。

目录列表是**透传**：A 不缓存、不合并、不解释。B 是唯一知道目录内容的一方，
A 多存一份就多一份"两边不一致"的可能。10s 超时是硬性的——前端展开一个节点
转十秒还不如直接报错让它重试。
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
            }
        )
    return {"items": items, "total": len(items)}


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
