"""服务端配置。

全部可用环境变量覆盖，前缀 `ROBOT_MONITOR_`，例如 `ROBOT_MONITOR_PORT=9000`。
生产环境不要依赖 .env，用 systemd 的 `Environment=` 或容器 secret。

管理员密码不在这里：它存在数据库的 `admin_user` 表里，首次 `serve` 时引导设置，
之后用 `robot-monitor-server passwd` 改。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# server/backend/src/robot_monitor_server/config.py -> server/
_SERVER_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ROBOT_MONITOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 监听 -----------------------------------------------------------
    # 生产环境建议绑 127.0.0.1，由 Nginx 反代并终止 TLS。
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # --- 运行时目录 -----------------------------------------------------
    data_dir: Path = Path("data")
    storage_dir: Path = Path("storage")

    # 前端构建产物。默认指向仓库内 server/frontend/dist，
    # 安装成 wheel 部署时用 ROBOT_MONITOR_FRONTEND_DIR 指到实际位置。
    frontend_dir: Path = _SERVER_ROOT / "frontend" / "dist"
    serve_frontend: bool = True

    # --- 单用户鉴权 -----------------------------------------------------
    admin_username: str = "admin"
    session_ttl_hours: int = 72
    cookie_name: str = "rm_session"
    # 本地 http 调试必须是 False；上 HTTPS 后改成 True。
    cookie_secure: bool = False

    # --- 上传分块 -------------------------------------------------------
    chunk_size_small: int = 1 * 1024 * 1024
    chunk_size_large: int = 8 * 1024 * 1024
    chunk_threshold: int = 512 * 1024 * 1024
    max_file_bytes: int = 20 * 1024**3
    upload_idle_minutes: int = 30
    # 建任务前要求磁盘剩余 >= total_size * ratio，避免传到 90% 才失败
    disk_reserve_ratio: float = 1.2

    # --- 批量取件（勾一个目录取整棵树）-----------------------------------
    # 一次展开的上限。超了就"部分展开"，让用户缩小范围——不做无上限遍历，
    # 否则误选一个挂载点会把 A 和 B 一起拖死。
    batch_max_files: int = 5000
    batch_max_depth: int = 32
    batch_timeout_seconds: float = 30.0
    # 后台建任务的并发。受 B 侧 MAX_CONCURRENT_UPLOADS（3）约束，
    # 这里高一些只是把 B 的上传队列填满，不会真的并发传 7 个。
    batch_create_concurrency: int = 7

    # --- 控制通道 -------------------------------------------------------
    command_timeout_seconds: float = 10.0
    # agent 每 15s 心跳一次，45s 没动静就判离线（容忍丢两拍）
    heartbeat_timeout_seconds: float = 45.0
    heartbeat_sweep_seconds: float = 5.0

    # --- 设备注册 -------------------------------------------------------
    # closed: 全部要人工批准（默认）——批准在网页「节点」页点，不用敲 CLI
    # first : 库里一台节点都没有时免审（第一台机器零配置接入）
    # open  : 任何请求都自动登记（完全可信的内网）
    enroll_policy: Literal["first", "open", "closed"] = "closed"
    enroll_rate_limit_per_minute: int = 20

    # --- 限流 -----------------------------------------------------------
    login_rate_limit_per_minute: int = 10
    task_rate_limit_per_minute: int = 30

    # --- 摄像头（媒体中继 + 云台）---------------------------------------
    # 单会话最多几个观众。共享同一路上游，所以云端出向带宽 = 码率 × 观众数。
    media_max_viewers: int = 4
    # 最后一个观众离开后多久关上游。刷新页面会瞬间断开再连上，不能立刻拆。
    media_idle_seconds: float = 10.0
    # 单会话最长时长。到点主动结束，防止一路看一天把带宽吃满。
    media_max_session_seconds: float = 1800.0
    # 等 cloud-agent 把媒体上游接进来的时限。
    media_command_timeout: float = 8.0
    # 单会话云台指令频率上限（次/秒），防止前端 bug 打爆机器人。
    media_ptz_rate_per_second: int = 20

    # --- 派生 -----------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / "robot-monitor.db"

    @property
    def staging_dir(self) -> Path:
        return self.storage_dir / "staging"

    @property
    def files_dir(self) -> Path:
        return self.storage_dir / "files"

    def chunk_size_for(self, total_bytes: int) -> int:
        """按文件大小选块大小。

        选小块的代价是请求数多（5GB/1MB = 5120 次 PUT），选大块的代价是
        单块失败要重传更多。512MB 是经验分界点。
        """
        return (
            self.chunk_size_large
            if total_bytes >= self.chunk_threshold
            else self.chunk_size_small
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.staging_dir, self.files_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
