"""取件任务：创建、查询、取消。

取消是"尽力而为"：B 可能已经断线，但 A 必须立刻把任务落到 canceled 并把
staging 删掉——否则用户以为取消了，磁盘却一直被占着。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..deps import Context, require_session
from ..protocol import ErrorCode
from ..services import TaskService

router = APIRouter(
    prefix="/api/v1/tasks",
    dependencies=[Depends(require_session)],
    tags=["取件任务"],
)


class CreateTaskPayload(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=4096)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(payload: CreateTaskPayload, request: Request, ctx: Context) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    if not ctx.limits.task.allow(client):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.RATE_LIMITED.value, "message": "任务创建过于频繁"},
        )
    return await ctx.tasks.create_task(payload.agent_id, payload.path)


@router.get("")
async def list_tasks(ctx: Context, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    return await ctx.tasks.list_tasks(limit=min(max(limit, 1), 500), offset=max(offset, 0))


@router.get("/{task_id}")
async def get_task(task_id: str, ctx: Context) -> dict[str, Any]:
    return await ctx.tasks.get_task(task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, ctx: Context) -> dict[str, Any]:
    return await ctx.tasks.cancel_task(task_id)


# TaskService 只用于类型提示，避免 linter 说 import 未使用
__all__ = ["TaskService", "router"]
