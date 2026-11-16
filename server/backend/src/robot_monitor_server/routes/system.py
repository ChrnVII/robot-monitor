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
        # 设备靠它认人：地址可以换，这个不会。见 db.py 里 instance 表的注释。
        # 公开在这里是刻意的——它不是凭据，只是让 B 能在"地址变了"时确认
        # "还是那一台"，从而不必把 token 发给一个还不确定是谁的地址。
        "instance_id": await ctx.db.instance_id(),
    }


@router.get("/api/v1/system/stats", dependencies=[Depends(require_session)])
async def stats(ctx: Context) -> dict[str, Any]:
    free = ctx.storage.free_bytes()
    total_files = await ctx.db.fetchval("SELECT COUNT(*) FROM files", default=0)
    used = await ctx.db.fetchval("SELECT COALESCE(SUM(size), 0) FROM files", default=0)
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


@router.get("/api/v1/system/metrics", dependencies=[Depends(require_session)])
async def metrics(ctx: Context) -> dict[str, Any]:
    """指标骨架（方案 §8.4）。

    原则：**能算的现在算，算不了的显式写成 null**。少一个字段会让看板把
    "没有采集"读成"数值是 0"——弱网问题上这两种解释的处置完全相反。

    命名与方案一致，媒体与事件两组在 P1/P3 接通后填进来，不用改前端字段名。
    """
    now = time.time()
    connections = ctx.hub.snapshot()

    control = {
        agent_id: info["stats"] for agent_id, info in connections.items()
    }
    return {
        "server_time": now,
        "control": {
            "per_agent": control,
            # 控制可靠性的核心指标：超时次数。它不代表"没执行"，只代表"没结果"。
            "control_unknown_total": sum(item["timeouts"] for item in control.values()),
            "requests_total": sum(item["requests"] for item in control.values()),
        },
        "state": {
            "per_agent": [
                {
                    "agent_id": record.agent_id,
                    "state_version": record.state_version,
                    "state_freshness_ms": round(max(0.0, now - record.observed_at) * 1000),
                    "freshness": record.freshness(now),
                }
                for record in (await ctx.states.snapshot()).values()
            ],
            **ctx.states.stats(),
        },
        # P1 接通媒体会话后：media_last_frame_age_ms / media_dropped_frames /
        # keyframe_waits / media_e2e_latency_ms（后者只能由浏览器上报）。
        "media": {
            "sessions": ctx.media.snapshot(),
        },
        # 补传落后量由**机器人自己**上报（它才知道本地还有多少条没发出去），
        # 随状态帧的 link 组一起上来。云端不去猜。
        "events": {
            "per_agent": [
                {
                    "agent_id": agent_id,
                    "event_lag_seq": (record.state.get("link") or {}).get("event_lag_seq"),
                }
                for agent_id, record in (await ctx.states.snapshot()).items()
            ]
        },
    }
