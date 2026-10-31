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


class PreviewPayload(BaseModel):
    """一次提交里用户选中的路径，可以是文件、目录，或混合。"""

    agent_id: str = Field(min_length=1, max_length=64)
    paths: list[str] = Field(min_length=1, max_length=500)


class BatchFile(BaseModel):
    rel: str = Field(min_length=1, max_length=4096)
    size: int = Field(default=0, ge=0)
    name: str | None = Field(default=None, max_length=255)


class BatchPayload(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    files: list[BatchFile] = Field(min_length=1, max_length=5000)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(payload: CreateTaskPayload, request: Request, ctx: Context) -> dict[str, Any]:
    client = request.client.host if request.client else "unknown"
    if not ctx.limits.task.allow(client):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.RATE_LIMITED.value, "message": "任务创建过于频繁"},
        )
    return await ctx.tasks.create_task(payload.agent_id, payload.path)


@router.post("/preview")
async def preview_selection(
    payload: PreviewPayload, request: Request, ctx: Context
) -> dict[str, Any]:
    """把选中的路径展开成文件清单，只统计、不建任务。

    前端拿它做"将取 128 个文件 / 3.2 GB"的确认框。
    """
    client = request.client.host if request.client else "unknown"
    if not ctx.limits.task.allow(client):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.RATE_LIMITED.value, "message": "操作过于频繁，请稍后再试"},
        )
    return await ctx.tasks.preview_selection(payload.agent_id, payload.paths)


@router.post("/batch", status_code=status.HTTP_202_ACCEPTED)
async def create_batch(
    payload: BatchPayload, request: Request, ctx: Context
) -> dict[str, Any]:
    """按预览得到的清单批量建任务。立即返回，任务在后台逐项创建。"""
    client = request.client.host if request.client else "unknown"
    if not ctx.limits.task.allow(client):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": ErrorCode.RATE_LIMITED.value, "message": "操作过于频繁，请稍后再试"},
        )
    return await ctx.tasks.create_tasks_from_files(
        payload.agent_id, [item.model_dump() for item in payload.files]
    )


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
