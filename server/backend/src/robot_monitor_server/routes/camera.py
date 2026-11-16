"""云台 / 预置点 / 快照：云端发起的相机操作。

三条纪律：

1. **一律经 `CommandService`。** 只有它构造信封（去重键、序号、deadline），
   只有它写 `command_log`。路由直接碰 `hub.request` 会绕过审计与状态机。
2. **快照返回二进制。** `POST .../camera/snapshot` 的响应体就是 `image/jpeg`，
   图像不进 JSON、不进控制帧。元信息走响应头。
3. **失败带上"现在是什么状态"。** 云台超时返回 504 + `state=unknown`：
   页面必须显示"结果未知"并给出"查一下预置点"的入口，而不是重试到天亮。

权限：本系统是单管理员租户，会话通过即等同于 `camera.view` + `camera.control`。
引入多角色时要在这里加 `Depends(require_permission("camera.control"))`——
控制点就这一个地方，不会散落。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from ..commands import CommandService
from ..deps import Context, SessionToken, require_session
from ..protocol import CommandName

router = APIRouter(
    prefix="/api/v1/agents/{agent_id}/camera",
    dependencies=[Depends(require_session)],
    tags=["摄像头"],
)

#: 与 `network_camera.py` 的方向枚举一致（zoom_in / zoom_out 也走同一张表）。
DIRECTIONS = {"up", "down", "left", "right", "zoom_in", "zoom_out", "stop"}


class PtzRequest(BaseModel):
    """方向键。

    `keepalive` 只更新 deadline、不改方向——按住不放时前端每 1.5s 发一条，
    而不是每秒新建一个动作。`stop` 拥有最高优先级，且不受 deadline 约束
    （agent 侧对 stop 只做去重，不做序号丢弃）。
    """

    action: Literal["start", "keepalive", "stop"] = "start"
    direct: str = Field(default="stop")
    speed: int = Field(default=4, ge=1, le=7)
    sessionId: str | None = Field(default=None, max_length=64)


class PresetGotoRequest(BaseModel):
    sessionId: str | None = Field(default=None, max_length=64)


class SnapshotRequest(BaseModel):
    sessionId: str | None = Field(default=None, max_length=64)


def _service(ctx: Context) -> CommandService:
    return ctx.commands


def ptz_payload(action: str, direct: str, speed: int) -> tuple[dict[str, Any] | None, str | None]:
    """把方向键请求翻成 agent 能用的 payload。返回 (payload, 错误信息)。

    这里最容易错的一条：**keepalive 只刷新 deadline，不改方向**。它必须沿用
    当前按住的那个方向。曾经写成"keepalive 一律按 stop 传"，结果是按住方向键
    1.5 秒后球机被自己发的 stop 停住，而页面还显示着"左转中"——那种故障在
    日志里完全看不出来，只能靠盯画面发现。
    """
    direction = (direct or "").lower()
    if direction not in DIRECTIONS:
        return None, f"不支持的云台方向：{direct!r}"
    if action == "keepalive" and direction == "stop":
        # 没有方向的 keepalive 是无意义的：调用方漏传了当前方向。明确拒绝，
        # 而不是把球机停掉。
        return None, "keepalive 必须带上当前方向"
    return {
        "action": action,
        "direct": "stop" if action == "stop" else direction,
        "speed": speed,
    }, None


@router.post("/ptz")
async def ptz(
    agent_id: str, body: PtzRequest, ctx: Context, token: SessionToken
) -> dict[str, Any]:
    payload, error = ptz_payload(body.action, body.direct, body.speed)
    if payload is None:
        return {"command_id": None, "state": "rejected", "error": error}
    return await _service(ctx).dispatch(
        agent_id,
        CommandName.CAMERA_PTZ,
        payload,
        session_id=body.sessionId or "",
        deadline_seconds=3.0,
    )


@router.get("/presets")
async def presets(
    agent_id: str, ctx: Context, token: SessionToken, sessionId: str | None = Query(default=None)
) -> dict[str, Any]:
    """预置点清单。以相机为准——本地缓存的名字只用于展示，不能当"有这个点"的证据。"""
    result = await _service(ctx).dispatch(
        agent_id,
        CommandName.CAMERA_PRESET_LIST,
        {},
        session_id=sessionId or "",
        deadline_seconds=6.0,
    )
    items = (result.get("result") or {}).get("items") or []
    return {"items": items, "total": len(items), "command_id": result["command_id"]}


@router.post("/presets/{preset}/goto")
async def goto_preset(
    agent_id: str, preset: int, body: PresetGotoRequest, ctx: Context, token: SessionToken
) -> dict[str, Any]:
    return await _service(ctx).dispatch(
        agent_id,
        CommandName.CAMERA_PRESET_GOTO,
        {"preset": preset},
        session_id=body.sessionId or "",
        deadline_seconds=6.0,
    )


@router.post("/snapshot")
async def snapshot(
    agent_id: str, ctx: Context, token: SessionToken, body: SnapshotRequest | None = None
) -> Response:
    """抓一张，直接把 JPEG 返回给页面。

    缓存策略由页面控制（弱网兜底时它自己决定多久刷一张），这里一律 `no-store`。
    """
    image, meta = await _service(ctx).request_snapshot(
        agent_id, "", session_id=(body.sessionId if body else None) or ""
    )
    return Response(
        content=image,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Camera-Bytes": str(meta.get("size") or len(image)),
            "X-Camera-Captured-At": str(meta.get("captured_at") or ""),
        },
    )


@router.get("/commands")
async def command_log(
    agent_id: str, ctx: Context, token: SessionToken, limit: int = Query(default=50, ge=1, le=200)
) -> dict[str, Any]:
    """最近的命令与终态。排障时先看这里：是没下发、被拒了，还是 unknown。"""
    rows = await _service(ctx).recent(agent_id, limit)
    return {"items": rows, "total": len(rows)}
