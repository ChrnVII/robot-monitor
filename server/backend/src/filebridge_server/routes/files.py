"""文件库：列表、搜索、下载、回收站。

下载交给 `FileResponse`：它已经处理了 Range（大文件续传）和 RFC 5987 的
中文文件名编码。自己拼 `Content-Disposition` 很容易在非 ASCII 文件名上炸
`UnicodeEncodeError`——header 只能是 latin-1。

但"能不能用"要靠测试钉住，不能靠记忆：`tests/test_upload_flow.py` 里有一条
`Range: bytes=0-99` 断言 206，Starlette 版本行为变了会立刻红。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..deps import Context, require_session
from ..protocol import ErrorCode

router = APIRouter(
    prefix="/api/v1/files",
    dependencies=[Depends(require_session)],
    tags=["文件库"],
)


@router.get("")
async def list_files(
    ctx: Context,
    q: str | None = None,
    deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    return await ctx.tasks.list_files(
        query=(q or "").strip() or None,
        deleted=deleted,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
    )


@router.get("/{file_id}/download")
async def download(file_id: str, ctx: Context) -> FileResponse:
    row, path = await ctx.tasks.storage_path_for(file_id)
    if not Path(path).exists():
        raise HTTPException(
            status.HTTP_410_GONE,
            detail={"code": ErrorCode.FILE_NOT_FOUND.value, "message": "磁盘上没有这个文件了"},
        )
    # 把哈希放进响应头，而不是让用户去界面上抄那 12 位前缀：
    # 校验发生在"拿到文件之后"，那就该和文件一起给。十六进制形式可以直接
    # 和 `sha256sum` 的输出对照（不用 RFC 9530 的 Digest，那个是 base64）。
    #
    # 注意它描述的是**整个文件**：Range 请求（206）也会带上同一个值，
    # 拿分片的字节去核对当然对不上，这是响应头的语义，不是 bug。
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=row["orig_name"],
        headers={"X-File-Sha256": str(row["sha256"])},
    )


@router.delete("/{file_id}")
async def delete_file(file_id: str, ctx: Context) -> dict[str, bool]:
    await ctx.tasks.soft_delete(file_id)
    return {"ok": True}


@router.post("/{file_id}/restore")
async def restore_file(file_id: str, ctx: Context) -> dict[str, bool]:
    await ctx.tasks.restore(file_id)
    return {"ok": True}


@router.delete("/{file_id}/purge")
async def purge_file(file_id: str, ctx: Context) -> dict[str, bool]:
    await ctx.tasks.purge(file_id)
    return {"ok": True}
