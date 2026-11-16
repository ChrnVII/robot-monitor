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
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthService
from .commands import CommandService
from .config import Settings, settings
from .db import Database
from .deps import AppContext, Limits
from .errors import ApiError, from_agent_error
from .events import EventBus
from .eventstore import EventStore
from .hub import AgentError, Hub
from .media import MediaRegistry
from .ratelimit import SlidingWindow
from .routes import agents, camera, enroll, events, files, system, tasks, uploads
from .routes import auth as auth_routes
from .routes import ingest as ingest_routes
from .routes import media as media_routes
from .services import TaskService
from .state import MemoryStateStore
from .storage import Storage
from .ws import router as ws_router

logger = logging.getLogger("robot-monitor.server")

MAINTENANCE_INTERVAL = 60.0
#: 状态快照落库间隔。状态是"现在是什么"，只在重启后需要一份初始值，
#: 所以低频覆盖写就够了；高频写库对 SQLite 是纯负担。
STATE_PERSIST_INTERVAL = 30.0


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
        states=MemoryStateStore(),
        media=MediaRegistry(),
        commands=CommandService(db, hub),
        events=EventStore(db),
    )
    app.state.ctx = ctx

    await task_service.recover_after_restart()
    await _restore_state(ctx)
    hub.start_sweeper()
    maintenance = asyncio.create_task(_maintenance(ctx), name="maintenance")
    persister = asyncio.create_task(_persist_state(ctx), name="state-persist")

    if not await auth.password_configured():
        logger.warning(
            "还没有设置管理员密码，网页端无法登录。"
            "用 `robot-monitor-server passwd` 设置（或用 `serve` 启动，它会引导）。"
        )
    logger.info(
        "robot-monitor %s ready: storage=%s data=%s frontend=%s",
        __version__,
        cfg.storage_dir.resolve(),
        cfg.data_dir.resolve(),
        cfg.frontend_dir,
    )

    try:
        yield
    finally:
        for task in (maintenance, persister):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await _save_state(ctx)
        # 先结束媒体会话：它会向 agent 发 camera.stream.stop，趁控制连接还在。
        await ctx.media.close_all("server shutting down")
        await hub.close_all()
        await hub.stop_sweeper()
        await db.close()


async def _restore_state(ctx: AppContext) -> None:
    """重启后先装回上次的状态快照。

    不装的话页面会从"—"开始，直到第一帧新状态到达——重启期间那台设备
    看起来像"从来没上报过"。装回来的那一帧 `observed_at` 还是旧的，
    所以新鲜度会如实显示成 stale/unknown，不会被当成实时数据。
    """
    rows = await ctx.db.fetchall(
        "SELECT agent_id, state_version, observed_at, received_at, state_json FROM agent_state"
    )
    for row in rows:
        try:
            state = json.loads(row["state_json"])
        except (TypeError, ValueError):
            continue
        put = getattr(ctx.states, "put_sync", None)
        if put is not None:
            put(str(row["agent_id"]), state, received_at=float(row["received_at"]))
    if rows:
        logger.info("已恢复 %d 台设备的最后状态快照", len(rows))


async def _save_state(ctx: AppContext) -> None:
    """把内存里的状态覆盖写进 agent_state（每台一行）。"""
    records = await ctx.states.snapshot()
    for agent_id, record in records.items():
        try:
            await ctx.db.execute(
                "INSERT INTO agent_state (agent_id, state_version, observed_at, received_at,"
                " state_json) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(agent_id) DO UPDATE SET state_version = excluded.state_version,"
                " observed_at = excluded.observed_at, received_at = excluded.received_at,"
                " state_json = excluded.state_json",
                (
                    agent_id,
                    record.state_version,
                    record.observed_at,
                    record.received_at,
                    json.dumps(record.state, ensure_ascii=False),
                ),
            )
        except Exception:
            logger.exception("状态快照落库失败：%s", agent_id)


async def _persist_state(ctx: AppContext) -> None:
    while True:
        await asyncio.sleep(STATE_PERSIST_INTERVAL)
        try:
            await _save_state(ctx)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("状态快照定时落库失败")


async def _maintenance(ctx: AppContext) -> None:
    """定期回收：空闲上传会话、过期会话、残留的打包临时文件。

    放在一个循环里而不是三个，是因为它们都是"每分钟做一次、做失败也不致命"
    的清理动作；分开只会多三个需要单独 cancel 的 task。
    """
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL)
        try:
            swept = await ctx.tasks.sweep_idle_uploads()
            purged_sessions = await ctx.auth.purge_expired_sessions()
            stale_archives = await ctx.tasks.sweep_archive_temp()
            if swept or purged_sessions or stale_archives:
                logger.info(
                    "maintenance: %d idle upload(s), %d session(s), %d stale archive(s)",
                    swept,
                    purged_sessions,
                    stale_archives,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance pass failed")


def create_app(config: Settings | None = None) -> FastAPI:
    cfg = config or settings

    app = FastAPI(
        title="Robot Monitor",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = cfg

    @app.exception_handler(ApiError)
    async def api_error_handler(_request, exc: ApiError) -> JSONResponse:
        content: dict[str, object] = {"code": exc.code.value, "message": exc.message}
        if exc.data is not None:
            content["data"] = exc.data
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(AgentError)
    async def agent_error_handler(_request, exc: AgentError) -> JSONResponse:
        converted = from_agent_error(exc)
        return JSONResponse(
            status_code=converted.status_code,
            content={"code": converted.code.value, "message": converted.message},
        )

    for module in (
        system,
        auth_routes,
        agents,
        tasks,
        files,
        uploads,
        events,
        enroll,
        camera,
        media_routes,
        ingest_routes,
    ):
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
