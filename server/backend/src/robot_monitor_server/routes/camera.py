"""摄像头 REST：画面状态、云台、预置点。

这里全部是**转发**，不解释业务：

- 画面状态来自 `MediaHub`（云端内存）；能力来自 agent 在 hello 里声明的 features；
- 云台与预置点转成控制通道命令，由 cloud-agent 去换本地租约、调 backend。

不在这里直接调机器人，是因为云端唯一允许的入口是控制通道；
一旦这里自己发 HTTP 到机器人，鉴权、超时、连接管理就会出现第二套。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import Context, require_session
from ..protocol import CommandName

router = APIRouter(
    prefix="/api/v1/agents/{agent_id}/camera",
    dependencies=[Depends(require_session)],
    tags=["摄像头"],
)

DEFAULT_CAMERA_ID = "ptz"

#: 单会话的云台指令时间戳。进程内即可——多 worker 本来就不支持（见 main.py）。
_ptz_window: dict[str, deque[float]] = {}


class PtzPayload(BaseModel):
    cameraId: str | None = Field(default=None, max_length=64)
    sessionId: str | None = Field(default=None, max_length=64)
    action: Literal["start", "stop"] = "start"
    direct: str = Field(min_length=1, max_length=16)
    speed: float = Field(default=0.5, ge=0.0, le=1.0)


def _camera_id(value: str | None) -> str:
    return (value or DEFAULT_CAMERA_ID).strip() or DEFAULT_CAMERA_ID


def _features(ctx: Context, agent_id: str) -> list[str]:
    conn = ctx.hub.get(agent_id)
    return list(conn.meta.features) if conn is not None else []


def _rate_limit(ctx: Context, key: str) -> None:
    limit = max(1, ctx.settings.media_ptz_rate_per_second)
    now = time.monotonic()
    # 会话结束不会回调到这里，所以按"最近一分钟没动静"顺手清一批，
    # 避免长跑进程里这个字典只增不减。
    if len(_ptz_window) > 256:
        for stale in [k for k, v in _ptz_window.items() if not v or now - v[-1] > 60]:
            _ptz_window.pop(stale, None)
    window = _ptz_window.setdefault(key, deque())
    while window and now - window[0] > 1.0:
        window.popleft()
    if len(window) >= limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": "云台指令过于频繁"},
        )
    window.append(now)


def _session_id(ctx: Context, agent_id: str, camera_id: str, given: str | None) -> str:
    # 以云端活动会话为准。页面在重连窗口里可能还留着上一轮的 sid，
    # 拿它去申请租约会换一个 holder，随后撞上"同优先级冲突"，
    # 表现成莫名其妙的"本地已接管"。
    active = ctx.media.status(agent_id, camera_id).get("sessionId")
    if active:
        return str(active)
    if given:
        return given
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": "no_active_session", "message": "当前没有观看会话，请先打开画面"},
    )


@router.get("/stream")
async def stream_status(
    ctx: Context,
    agent_id: str,
    cameraId: str | None = None,
) -> dict[str, Any]:
    camera_id = _camera_id(cameraId)
    features = _features(ctx, agent_id)
    return {
        "agentId": agent_id,
        "cameraId": camera_id,
        "agentOnline": ctx.hub.get(agent_id) is not None,
        "capabilities": features,
        "supported": "camera.media.v1" in features,
        "ptzSupported": "camera.ptz.v1" in features,
        "stream": ctx.media.status(agent_id, camera_id),
    }


@router.delete("/stream")
async def stop_stream(
    ctx: Context,
    agent_id: str,
    cameraId: str | None = None,
) -> dict[str, Any]:
    camera_id = _camera_id(cameraId)
    result = await ctx.media.stop(agent_id, camera_id)
    await ctx.auth.audit("web", "camera.view.stop", target=agent_id, detail=camera_id)
    return result


@router.post("/ptz")
async def ptz(
    ctx: Context,
    agent_id: str,
    body: PtzPayload,
) -> dict[str, Any]:
    camera_id = _camera_id(body.cameraId)
    session_id = _session_id(ctx, agent_id, camera_id, body.sessionId)
    # 停止永远放行：限流只约束"开始动作"，否则用户快速连点后松手会被 429，
    # 球机就会一直转到 agent 的看护超时。
    if body.action != "stop" and body.direct != "stop":
        _rate_limit(ctx, f"{agent_id}:{session_id}")
    result = await ctx.hub.request(
        agent_id,
        CommandName.CAMERA_PTZ,
        {
            "cameraId": camera_id,
            "sessionId": session_id,
            "action": body.action,
            "direct": body.direct,
            "speed": body.speed,
        },
        timeout=ctx.settings.media_command_timeout,
    )
    return result


@router.get("/presets")
async def list_presets(
    ctx: Context,
    agent_id: str,
    cameraId: str | None = None,
) -> dict[str, Any]:
    result = await ctx.hub.request(
        agent_id,
        CommandName.CAMERA_PRESET_LIST,
        {"cameraId": _camera_id(cameraId)},
        timeout=ctx.settings.media_command_timeout,
    )
    items = result.get("items") if isinstance(result, dict) else None
    return {"items": items if isinstance(items, list) else []}


@router.post("/presets/{preset}/goto")
async def goto_preset(
    ctx: Context,
    agent_id: str,
    preset: int,
    cameraId: str | None = None,
    sessionId: str | None = None,
) -> dict[str, Any]:
    if preset < 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_arguments", "message": "预置点编号必须为正整数"},
        )
    camera_id = _camera_id(cameraId)
    sid = _session_id(ctx, agent_id, camera_id, sessionId)
    _rate_limit(ctx, f"{agent_id}:{sid}")
    result = await ctx.hub.request(
        agent_id,
        CommandName.CAMERA_PRESET_GOTO,
        {"cameraId": camera_id, "sessionId": sid, "preset": preset},
        timeout=ctx.settings.media_command_timeout,
    )
    await ctx.auth.audit("web", "camera.preset.goto", target=agent_id, detail=f"{camera_id}:{preset}")
    return result
