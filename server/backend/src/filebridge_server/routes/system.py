"""健康检查、运行状态与审计日志。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from .. import __version__
from ..deps import Context, require_session
from ..protocol import PROTOCOL_VERSION

router = APIRouter(tags=["系统"])


@router.get("/api/v1/health")
async def health(ctx: Context) -> dict[str, Any]:
    storage_ok = ctx.settings.storage_dir.exists() and ctx.settings.storage_dir.is_dir()
    return {
        "status": "ok" if storage_ok else "degraded",
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "storage_ready": storage_ok,
        "auth_configured": await ctx.auth.password_configured(),
        "agents_online": len(ctx.hub.online_ids()),
    }


@router.get("/api/v1/system/stats", dependencies=[Depends(require_session)])
async def stats(ctx: Context) -> dict[str, Any]:
    free = ctx.storage.free_bytes()
    total_files = await ctx.db.fetchval(
        "SELECT COUNT(*) FROM files WHERE deleted_at IS NULL", default=0
    )
    trashed = await ctx.db.fetchval(
        "SELECT COUNT(*) FROM files WHERE deleted_at IS NOT NULL", default=0
    )
    used = await ctx.db.fetchval(
        "SELECT COALESCE(SUM(size), 0) FROM files WHERE deleted_at IS NULL", default=0
    )
    # 侧栏「节点」上的小红点靠它。放在 stats 里而不是单独一个接口：
    # AppLayout 本来就在轮询 stats，多一个字段比多一次请求便宜。
    pending = await ctx.db.fetchval(
        "SELECT COUNT(*) FROM enroll_requests WHERE status = 'pending'", default=0
    )
    return {
        "version": __version__,
        "protocol": PROTOCOL_VERSION,
        "disk_free_bytes": free,
        "files": int(total_files),
        "trashed": int(trashed),
        "library_bytes": int(used),
        "agents_online": len(ctx.hub.online_ids()),
        "pending_enrollments": int(pending),
        "sse_subscribers": ctx.bus.subscriber_count,
        "server_time": time.time(),
    }


@router.get("/api/v1/system/audit", dependencies=[Depends(require_session)])
async def audit(ctx: Context, limit: int = 100) -> dict[str, Any]:
    rows = await ctx.auth.recent_audit(limit=min(max(limit, 1), 500))
    return {"items": rows, "total": len(rows)}
