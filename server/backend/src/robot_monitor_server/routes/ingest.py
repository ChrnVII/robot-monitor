"""机器人侧的上报入口（C2）：事件批量。

为什么事件不走控制连接：断网十分钟后要补几百上千条，这批数据一旦占住 C0，
用户的"停止"命令就排在补传后面了。所以这里是一条独立的 HTTP 通道，
鉴权复用 agent token，与 `/internal/uploads` 同一套。

两个必须守住的语义：

- **重复不报错。** at-least-once 投递 + 幂等消费。机器人补传窗口重叠、
  或重连后重发，都是正常现象，返回 `duplicates` 计数即可。
- **`acked_seq` 只前进不后退。** 机器人据此删本地行；回一个比上次小的值
  会让它把已经删掉的事件再发一遍，白耗带宽。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..deps import AgentId, AppContext

logger = logging.getLogger("robot-monitor.ingest")

router = APIRouter(tags=["内部"])


class EventBatch(BaseModel):
    agent_id: str | None = Field(default=None, max_length=64)
    cursor: int = Field(default=0, ge=0)
    # 上限与 agent 的 BATCH_LIMIT 对齐：再大就不再是"批量"，而是一次灌库。
    batch: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


@router.post("/internal/events")
async def ingest_events(body: EventBatch, request: Request, agent_id: AgentId) -> dict[str, Any]:
    """收一批设备事件。`agent_id` 以 token 为准，不信请求体里的。"""
    ctx: AppContext = request.app.state.ctx
    if body.agent_id and body.agent_id != agent_id:
        # 一台机器只能给自己的事件：否则被攻破的 agent 能往别人的时间线上写。
        logger.warning(
            "agent %s 试图以 %s 的身份上报事件，已按 token 归属处理", agent_id, body.agent_id
        )
    result = await ctx.events.ingest(agent_id, body.batch, cursor=body.cursor)
    if body.batch:
        # SSE 只是提醒"有新事件"；真正的数据靠游标查询补齐。
        ctx.bus.publish(
            "device_event",
            {"agent_id": agent_id, "acked_seq": result["acked_seq"], "received": result["received"]},
        )
    return {**result, "server_time": time.time()}
