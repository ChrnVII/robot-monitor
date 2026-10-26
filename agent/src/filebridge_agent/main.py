"""agent 命令行入口。

    filebridge-agent --server https://files.example.com --root /srv/share

首次会自动向 A 注册并把凭据存到本地（0600），之后连参数都可以省。

B 上不要求装 git 和源码：`uv build` 出 wheel 后 `uv tool install` 即可（见 README）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .client import AgentClient
from .config import ConfigError, load_config
from .enroll import EnrollError, ensure_credentials
from .sandbox import Sandbox, SandboxError


def require_linux(platform_name: str | None = None) -> None:
    """生产入口只接受 Linux。不是 "linux" 就直接退出。

    这不是技术限制——代码本身在别的系统上多半能跑起来，那个更危险。路径沙箱、
    只读挂载、systemd 的 ReadOnlyPaths 都按 POSIX 语义设计：反斜杠是不是普通
    文件名字符、软链接逃逸能不能被验证、保留设备名会不会卡住读线程，这些在
    非 POSIX 文件系统上都有不同答案，而"看起来能跑"会让人以为它是对的。

    单元测试与端到端测试不经过这个入口，所以在任意开发机上仍然可以跑。
    """
    name = platform_name if platform_name is not None else sys.platform
    if name == "linux":
        return
    print(
        f"FileBridge agent 只支持 Linux，当前平台是 {name}。\n"
        "A 与 B 的目标平台都是 Linux；非 Linux 上的行为不做保证。\n"
        "（单元测试与端到端测试不走这个入口，可跨平台运行。）",
        file=sys.stderr,
    )
    raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filebridge-agent",
        description="把本机目录通过 FileBridge 共享给 A（只读）",
    )
    parser.add_argument("--server", default=None, help="A 的地址，https:// 或 wss:// 均可")
    parser.add_argument("--token", default=None, help="设备 token（server 端 issue-token 签发）")
    parser.add_argument("--root", default=None, help="要共享的目录（只读）")
    parser.add_argument("--name", default=None, help="本机名称，仅用于 A 侧展示")
    parser.add_argument("--config", default=None, help="TOML 配置文件路径")
    parser.add_argument("--log-dir", default=None, help="日志目录（不传则只输出到 stderr）")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="凭据文件目录，默认项目内 .filebridge/",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="同时上传的分片数，默认 3（再高会让 A 的磁盘寻道成为瓶颈）",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 TLS 证书校验（只用于自签证书的内网测试）",
    )
    return parser


def setup_logging(level: str, log_dir: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(directory / "agent.log", encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    require_linux()

    try:
        config = load_config(
            args.config,
            server=args.server,
            token=args.token,
            root=args.root,
            name=args.name,
            log_dir=args.log_dir,
            log_level=args.log_level,
            state_dir=args.state_dir,
            max_concurrent_uploads=args.concurrency,
            verify_tls=False if args.insecure else None,
        )
        config.validate()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        sys.exit(2)

    setup_logging(config.log_level, config.log_dir)
    logger = logging.getLogger("filebridge.agent")

    try:
        asyncio.run(_run(config))
    except (ConfigError, EnrollError, SandboxError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        logger.info("收到中断，退出")


async def _run(config) -> None:
    # 先补齐 server / token / root（必要时向 A 注册），再校验共享目录
    config = await ensure_credentials(config)
    sandbox = Sandbox(config.root)

    logger = logging.getLogger("filebridge.agent")
    logger.info(
        "共享目录 %s，连接 %s（HTTP %s）", sandbox.root, config.ws_url, config.http_base
    )

    client = AgentClient(config, sandbox)
    loop = asyncio.get_running_loop()
    # 只在 Linux 上跑（require_linux 已经拦过），SIGTERM 的 loop handler 一定可用。
    # 这里不写 Windows 的兜底分支：目标平台之外的行为不打算保证，写了反而是假装支持。
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, client.stop)
    await client.run()


if __name__ == "__main__":
    main()
