"""B 专用的分片上传端点。

三个必须做的检查，少一个就是洞：

1. **`Content-Length` 必须存在，且与实际读到的字节数一致。** 不校验声明值，
   一个块可以塞满磁盘；不校验实际值，声明 1MB 实发 10MB 也能写进去。
2. **upload 的归属必须与 token 对应的 agent 一致。** 否则 A 上并存两台 B 时，
   一台能往另一台的任务里写块。
3. **块长度必须等于 `chunk_size`（最后一块除外）。** 长度不对直接 400，
   不要让 A 侧出现"文件大小对但内容错位"的状态。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..deps import AgentId, Context
from ..protocol import ErrorCode

router = APIRouter(prefix="/internal/uploads", tags=["agent 上传"])


class CompletePayload(BaseModel):
    sha256: str | None = Field(default=None, max_length=64)


@router.put("/{upload_id}/chunks/{index}")
async def put_chunk(
    upload_id: str, index: int, request: Request, agent_id: AgentId, ctx: Context
) -> dict[str, Any]:
    upload = await ctx.tasks.get_upload(upload_id)
    if upload is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": ErrorCode.FILE_NOT_FOUND.value, "message": "upload 不存在或已回收"},
        )
    if upload["agent_id"] != agent_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": ErrorCode.FORBIDDEN.value, "message": "这个 upload 不属于当前 agent"},
        )

    declared = request.headers.get("content-length")
    if declared is None:
        raise HTTPException(
            status.HTTP_411_LENGTH_REQUIRED,
            detail={
                "code": ErrorCode.INVALID_ARGUMENTS.value,
                "message": "分片必须带 Content-Length；不支持 chunked 传输",
            },
        )
    try:
        declared_bytes = int(declared)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": ErrorCode.INVALID_ARGUMENTS.value, "message": "Content-Length 不是整数"},
        ) from exc

    chunk_size = int(upload["chunk_size"])
    total_size = int(upload["total_size"])
    offset = index * chunk_size
    expected = max(0, min(chunk_size, total_size - offset))

    if declared_bytes > chunk_size:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": ErrorCode.INVALID_ARGUMENTS.value,
                "message": f"分片 {declared_bytes} 字节超过 chunk_size {chunk_size}",
            },
        )
    if declared_bytes != expected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": ErrorCode.INVALID_ARGUMENTS.value,
                "message": f"第 {index} 块应为 {expected} 字节，声明了 {declared_bytes}",
            },
        )

    body = await request.body()
    if len(body) != declared_bytes:
        # 连接中断或代理改写了长度，这一块不可信
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": ErrorCode.INVALID_ARGUMENTS.value,
                "message": f"实际收到 {len(body)} 字节，与声明的 {declared_bytes} 不符",
            },
        )

    return await ctx.tasks.add_chunk(upload_id, index, body)


@router.get("/{upload_id}/status")
async def upload_status(upload_id: str, agent_id: AgentId, ctx: Context) -> dict[str, Any]:
    upload = await ctx.tasks.get_upload(upload_id)
    if upload is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": ErrorCode.FILE_NOT_FOUND.value, "message": "upload 不存在或已回收"},
        )
    if upload["agent_id"] != agent_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": ErrorCode.FORBIDDEN.value, "message": "这个 upload 不属于当前 agent"},
        )
    return await ctx.tasks.upload_status(upload_id)


@router.post("/{upload_id}/complete")
async def complete(
    upload_id: str, payload: CompletePayload, agent_id: AgentId, ctx: Context
) -> dict[str, Any]:
    upload = await ctx.tasks.get_upload(upload_id)
    if upload is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": ErrorCode.FILE_NOT_FOUND.value, "message": "upload 不存在或已回收"},
        )
    if upload["agent_id"] != agent_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": ErrorCode.FORBIDDEN.value, "message": "这个 upload 不属于当前 agent"},
        )
    return await ctx.tasks.complete_upload(upload_id, payload.sha256)
