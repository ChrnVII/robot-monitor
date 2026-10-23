"""FastAPI 入口。

关于 worker 数的硬约束：WebSocket 连接注册表是**进程内内存**。如果 uvicorn 起
多个 worker，C 的请求可能落到 worker A，而 B 的连接挂在 worker B，A 永远找不到
那台机器，表现为"节点在线但所有指令超时"——这类问题排查起来极其费时。

所以 v1 强制单 worker，并在启动时显式写死。将来要横向扩，必须先把注册表搬到
Redis pub/sub，而不是简单加 --workers。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthService
from .config import Settings, settings
from .db import Database
from .deps import AppContext, Limits
from .errors import ApiError, from_agent_error
from .events import EventBus
from .hub import AgentError, Hub
from .ratelimit import SlidingWindow
from .routes import agents, enroll, events, files, system, tasks, uploads
from .routes import auth as auth_routes
from .services import TaskService
from .storage import Storage
from .ws import router as ws_router

logger = logging.getLogger("filebridge.server")

MAINTENANCE_INTERVAL = 60.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 从 app.state 取配置，而不是模块级单例——否则测试注入的临时目录不生效，
    # 会往仓库目录里建 data/ 和 storage/。
    cfg: Settings = app.state.settings
    cfg.ensure_dirs()

    db = Database(cfg.db_path)
    await db.connect()

    auth = AuthService(db, cfg)
    bus = EventBus()
    hub = Hub(auth, bus, cfg)
    storage = Storage(cfg)
    task_service = TaskService(db, cfg, hub, storage, bus, auth)
    ctx = AppContext(
        settings=cfg,
        db=db,
        auth=auth,
        hub=hub,
        storage=storage,
        bus=bus,
        tasks=task_service,
        limits=Limits(
            login=SlidingWindow(cfg.login_rate_limit_per_minute),
            task=SlidingWindow(cfg.task_rate_limit_per_minute),
            enroll=SlidingWindow(cfg.enroll_rate_limit_per_minute),
        ),
    )
    app.state.ctx = ctx

    await task_service.recover_after_restart()
    hub.start_sweeper()
    maintenance = asyncio.create_task(_maintenance(ctx), name="maintenance")

    if not await auth.password_configured():
        logger.warning(
            "还没有设置管理员密码，网页端无法登录。"
            "用 `filebridge-server passwd` 设置（或用 `serve` 启动，它会引导）。"
        )
    logger.info(
        "filebridge %s ready: storage=%s data=%s frontend=%s",
        __version__,
        cfg.storage_dir.resolve(),
        cfg.data_dir.resolve(),
        cfg.frontend_dir,
    )

    try:
        yield
    finally:
        maintenance.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance
        await hub.close_all()
        await hub.stop_sweeper()
        await db.close()


async def _maintenance(ctx: AppContext) -> None:
    """定期回收：空闲上传会话、过期会话、超期回收站文件。

    放在一个循环里而不是三个，是因为它们都是"每分钟做一次、做失败也不致命"
    的清理动作；分开只会多三个需要单独 cancel 的 task。
    """
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL)
        try:
            swept = await ctx.tasks.sweep_idle_uploads()
            purged_sessions = await ctx.auth.purge_expired_sessions()
            purged_files = await ctx.tasks.purge_expired_trash()
            if swept or purged_sessions or purged_files:
                logger.info(
                    "maintenance: %d idle upload(s), %d session(s), %d trashed file(s)",
                    swept,
                    purged_sessions,
                    purged_files,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance pass failed")


def create_app(config: Settings | None = None) -> FastAPI:
    cfg = config or settings

    app = FastAPI(
        title="FileBridge",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = cfg

    @app.exception_handler(ApiError)
    async def api_error_handler(_request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code.value, "message": exc.message},
        )

    @app.exception_handler(AgentError)
    async def agent_error_handler(_request, exc: AgentError) -> JSONResponse:
        converted = from_agent_error(exc)
        return JSONResponse(
            status_code=converted.status_code,
            content={"code": converted.code.value, "message": converted.message},
        )

    for module in (system, auth_routes, agents, tasks, files, uploads, events, enroll):
        app.include_router(module.router)
    app.include_router(ws_router)

    _install_frontend(app, cfg)
    return app


def _install_frontend(app: FastAPI, cfg: Settings) -> None:
    """托管 Vue 构建产物。

    开发时前端跑 Vite dev server，走 proxy；生产时只需要 Python 运行时，
    `dist` 由这里直接服务。dist 不存在也不报错，只是没有界面——
    这比让服务起不来好。
    """
    dist = Path(cfg.frontend_dir)
    index = dist / "index.html"
    if not (cfg.serve_frontend and index.is_file()):
        logger.info("前端产物不存在（%s），只提供 API", dist)
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    root = dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        # 未知的 API 路径必须 404，不能落回 index.html——否则前端拿到的
        # "HTML 当 JSON 解析"报错会掩盖真正的 404。
        if full_path.startswith(("api/", "internal/", "ws/")):
            raise HTTPException(status_code=404, detail="not found")
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and root in candidate.parents:
                return FileResponse(candidate)
        return FileResponse(index)


app = create_app()


def main() -> None:
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
