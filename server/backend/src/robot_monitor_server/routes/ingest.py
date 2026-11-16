"""机器人侧的上报入口（C2）：事件批量。

为什么事件不走控制连接：断网十分钟后要补几百上千条，这批数据一旦占住 C0，
用户的"停止"命令就排在补传后面了。所以这里是一条独立的 HTTP 通道，
鉴权复用 agent token，与 `/internal/uploads` 同一套。

三个必须守住的语义：

- **重复不报错。** at-least-once 投递 + 幂等消费。机器人补传窗口重叠、
  或重连后重发，都是正常现象，返回 `duplicates` 计数即可。
- **`acked_seq` 只前进不后退。** 机器人据此删本地行；回一个比上次小的值
  会让它把已经删掉的事件再发一遍，白耗带宽。
- **事件不只是流水账。** 带状态迁移的事件（如 `upload_failed`）落库之后还要
  真的执行那次迁移，否则"补传成功"只等于"记了一笔账"。
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

#: 落库之后还必须改变云端状态的事件类型。
#:
#: `upload_failed` 要把那条上传标成失败，否则页面上会挂着一个永远不动的进度条。
#: C0 上的 EVENT 帧也能触发同一件事（`services.handle_agent_event`），但那是
#: 尽力而为的（outbox 满即丢）——而事件失败的常见原因恰恰就是 C0 断了。所以
#: 补传上来的这批必须同样能生效，否则"事件不丢"只做到了记账，没做到收敛。
SIDE_EFFECT_EVENTS = frozenset({"upload_failed"})


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
    if result.get("dropped"):
        # 有事没入库就必须留一行：否则事件链路的"不丢"只是口号。
        logger.warning(
            "agent %s 本批有 %d 条事件未入库（超出单批上限或无 event_seq）",
            agent_id,
            result["dropped"],
        )
    if body.batch:
        # 副作用不看 `received`：整批都是重复时也要再走一遍。上一轮可能"入库成功、
        # 副作用前崩了"，而这一步是幂等的，重放一遍才是真正的收敛。
        await apply_side_effects(ctx, agent_id, body.batch)
        # SSE 只是提醒"有新事件"；真正的数据靠游标查询补齐。
        ctx.bus.publish(
            "device_event",
            {
                "agent_id": agent_id,
                "acked_seq": result["acked_seq"],
                "received": result["received"],
                "dropped": result.get("dropped", 0),
            },
        )
    return {**result, "server_time": time.time()}


async def apply_side_effects(
    ctx: AppContext, agent_id: str, batch: list[dict[str, Any]]
) -> None:
    """把带状态迁移的事件交给与 C0 完全相同的那条处理路径。

    直接复用 `TaskService.handle_agent_event` 而不是在这里再写一遍：两条通道对
    同一件事必须得出同一个结论，复制一份判断迟早会变成两套。它是幂等的
    （只在 `status == uploading` 时才动手），所以 C0 的 EVENT 帧与 C2 的补传
    重复到达时不会二次生效。

    失败只记日志、不牵连入库：事件已经落库，回放随时可以再来一次；
    为了副作用失败把整批退回给机器人重发，反而会把正确的事做两遍。
    """
    for item in batch:
        event_type = str(item.get("event_type") or "")
        if event_type not in SIDE_EFFECT_EVENTS:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        try:
            await ctx.tasks.handle_agent_event(
                agent_id, {**payload, "kind": payload.get("kind") or event_type}
            )
        except Exception:  # 副作用失败不该让整批上报失败
            logger.exception(
                "事件 %s 已入库，但对应的状态迁移执行失败（可重放）", event_type
            )
