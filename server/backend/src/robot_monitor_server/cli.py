"""命令行入口。

日常只需要一条命令：

    robot-monitor-server serve        # 首次会引导设置管理员密码

其余命令都是"出了情况才用"：`passwd` 改密码、`pending` / `approve` 处理新设备、
`revoke-token` 吊销、`list-agents` 查看、`dedup` 清理历史重复文件。

`issue-token` 保留为手工通道：自动化脚本、或者想跳过注册审批直接发凭据时用，
默认流程不需要它。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .auth import AuthService
from .config import settings
from .db import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robot-monitor-server",
        description="Robot Monitor A 端服务：WebSocket 控制通道 + 文件库",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动服务（默认命令）")
    serve.add_argument("--host", default=None, help="监听地址，默认取 ROBOT_MONITOR_HOST")
    serve.add_argument("--port", type=int, default=None, help="端口，默认 8000")
    serve.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")

    it = sub.add_parser("issue-token", help="签发或轮换一台 agent 的 token")
    it.add_argument("--name", required=True, help="agent 名称，例如 b-office")
    it.add_argument("--hostname", default=None, help="备注用的主机名")
    it.add_argument("--os", dest="os_name", default=None, help="操作系统描述")

    rt = sub.add_parser("revoke-token", help="吊销一台 agent 的 token")
    rt.add_argument("agent_id")

    sub.add_parser("list-agents", help="列出已注册的 agent")

    sub.add_parser("passwd", help="修改管理员密码（立即生效，不需要重启）")
    sub.add_parser("pending", help="列出待批准的设备注册申请")

    approve = sub.add_parser("approve", help="批准一个待注册设备")
    approve.add_argument("pending_id", help="注册申请 ID（可用前缀）")

    reject = sub.add_parser("reject", help="拒绝一个待注册设备")
    reject.add_argument("pending_id", help="注册申请 ID（可用前缀）")

    dedup = sub.add_parser("dedup", help="查看（并可选合并）文件库里的重复内容")
    dedup.add_argument(
        "--apply",
        action="store_true",
        help="真的执行：把多余副本换成指向同一份数据的硬链接，释放磁盘",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        run_server(host=args.host, port=args.port, reload=args.reload)
    elif command == "issue-token":
        asyncio.run(_issue_token(args.name, args.hostname, args.os_name))
    elif command == "revoke-token":
        asyncio.run(_revoke_token(args.agent_id))
    elif command == "list-agents":
        asyncio.run(_list_agents())
    elif command == "passwd":
        asyncio.run(_passwd())
    elif command == "pending":
        asyncio.run(_pending())
    elif command == "approve":
        asyncio.run(_set_enroll(args.pending_id, "approved"))
    elif command == "reject":
        asyncio.run(_set_enroll(args.pending_id, "rejected"))
    elif command == "dedup":
        asyncio.run(_dedup(apply=args.apply))
    else:  # pragma: no cover - argparse 已经拦住了
        parser.print_help()
        sys.exit(2)


def run_server(*, host: str | None, port: int | None, reload: bool) -> None:
    import uvicorn

    if host:
        settings.host = host
    if port:
        settings.port = port

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.ensure_dirs()

    # 起服务之前把管理员密码准备好：没有就当场引导，不需要手工算 hash。
    asyncio.run(_ensure_admin_password())

    uvicorn.run(
        "robot_monitor_server.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=reload,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


async def _with_db(action: Any) -> Any:
    settings.ensure_dirs()
    # 把实际用的库路径打出来。这只为防一个具体的坑：在 systemd 部署里跑 CLI 却
    # 忘了加载 /etc/robot-monitor/robot-monitor.env，配置会退化成相对路径，于是悄悄
    # 在当前目录新建一个空库——token 签进了错的地方，表现是"agent 一直连不上，
    # 但 list-agents 里明明有它"。
    print(f"[db] {settings.db_path.resolve()}", file=sys.stderr)
    db = Database(settings.db_path)
    await db.connect()
    try:
        return await action(db, AuthService(db, settings))
    finally:
        await db.close()


async def _issue_token(name: str, hostname: str | None, os_name: str | None) -> None:
    async def action(db: Database, auth: AuthService) -> None:
        agent_id, token = await auth.issue_agent_token(name, hostname=hostname, os_name=os_name)
        print(f"agent_id: {agent_id}")
        print(f"token:    {token}")
        print(
            "\ntoken 只显示这一次，库里只存 sha256，丢了只能重新签发。\n"
            "在 B 上配置：\n"
            f"  robot-monitor-agent --server https://<你的域名> --token {token} --root /path/to/share",
            file=sys.stderr,
        )

    await _with_db(action)


async def _revoke_token(agent_id: str) -> None:
    async def action(db: Database, auth: AuthService) -> None:
        if await auth.revoke_agent(agent_id):
            print(f"已吊销 {agent_id}，该 token 立即失效")
        else:
            print(f"没有这个 agent: {agent_id}", file=sys.stderr)
            sys.exit(1)

    await _with_db(action)


async def _list_agents() -> None:
    async def action(db: Database, auth: AuthService) -> None:
        rows = await db.fetchall("SELECT * FROM agents ORDER BY created_at")
        if not rows:
            print("还没有任何 agent，用 issue-token 签一个")
            return
        for row in rows:
            state = "revoked" if row["revoked_at"] else ("online" if row["online"] else "offline")
            print(f"{row['id']:<14} {row['name']:<20} {state:<8} {row['hostname'] or '-'}")

    await _with_db(action)



# --- 重复内容 -----------------------------------------------------------
#
# 现在的模型是"记录按路径、磁盘按内容"：同一份内容出现在两个目录下是正常的，
# 它们本来就该共用一个 inode。所以这条命令只找**还没共享存储**的历史数据——
# 判重上线之前取的那些文件，两个路径各占一份盘。
#
# 合并方式是硬链接，一行都不删：用户按名字、按路径记住的入口都还在。


async def _dedup(*, apply: bool) -> None:
    async def action(db: Database, auth: AuthService) -> None:
        groups = await _duplicate_groups(db)
        if not groups:
            print("没有重复内容")
            return

        reclaimable = 0
        freed = 0
        for group in groups:
            rows = await db.fetchall(
                "SELECT * FROM files WHERE agent_id = ? AND sha256 = ?"
                " ORDER BY created_at",
                (group["agent_id"], group["sha256"]),
            )
            if _shares_storage([row["stored_path"] for row in rows]):
                # 已经共用一份数据了：两条记录本来就是我们想要的形态
                continue

            keeper, copies = rows[0], rows[1:]
            wasted = int(group["size"]) * len(copies)
            reclaimable += wasted
            print(
                f"\n{group['sha256'][:12]}  {len(rows)} 份 · 每份 {_human(group['size'])}"
                f" · 可释放 {_human(wasted)}"
            )
            print(f"  保留  {keeper['orig_name']}  ({keeper['rel_path'] or '—'})")
            for row in copies:
                print(f"  重复  {row['orig_name']}  ({row['rel_path'] or '—'})")
                if not apply:
                    continue
                if _link_over(settings.storage_dir, keeper["stored_path"], row["stored_path"]):
                    freed += int(group["size"])
                else:
                    print("        跳过：找不到文件，或这个文件系统不支持硬链接")

        if apply:
            print(f"\n已合并，释放 {_human(freed)}")
        elif reclaimable:
            print(f"\n合计可释放 {_human(reclaimable)}。加 --apply 真的执行。")
        else:
            print("所有重复内容都已共用同一份数据，没有可释放的空间")

    await _with_db(action)


def _shares_storage(paths: list[str]) -> bool:
    """这些落盘路径是不是已经指向同一个 inode。"""
    inodes = set()
    for rel in paths:
        try:
            inodes.add((settings.storage_dir / rel).stat().st_ino)
        except OSError:
            return False
    return len(inodes) == 1


async def _duplicate_groups(db: Database) -> list[Any]:
    """按 (节点, sha256) 找重复。

    排序按"能释放的空间"倒序，先处理大头。
    """
    return await db.fetchall(
        "SELECT agent_id, sha256, COUNT(*) AS copies, MIN(size) AS size"
        " FROM files"
        " GROUP BY agent_id, sha256 HAVING COUNT(*) > 1"
        " ORDER BY size * (COUNT(*) - 1) DESC"
    )


def _link_over(base: Any, keeper: str, target: str) -> bool:
    """把 target 换成指向 keeper 的硬链接。用临时名 + replace，不留半截状态。"""
    source = Path(base) / keeper
    destination = Path(base) / target
    if not source.exists() or not destination.exists():
        return False

    staging = destination.with_name(f"{destination.name}.dedup")
    try:
        staging.unlink(missing_ok=True)
        os.link(source, staging)
        os.replace(staging, destination)
        return True
    except OSError:
        staging.unlink(missing_ok=True)
        return False


def _human(value: Any) -> str:
    size = float(value or 0)
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"
    return f"{size:.0f} TB"


# --- 管理员密码 ---------------------------------------------------------

PASSWORD_MIN_LENGTH = 8
# 去掉 l / i / o / 0 / 1：这几个字符在终端里抄错率最高
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _generate_password(groups: int = 4, size: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


def _prompt_new_password() -> str:
    while True:
        first = getpass.getpass("密码（至少 8 位）: ")
        if len(first) < PASSWORD_MIN_LENGTH:
            print(f"至少 {PASSWORD_MIN_LENGTH} 位，再来一次", file=sys.stderr)
            continue
        second = getpass.getpass("再输一次: ")
        if first != second:
            print("两次输入不一致，再来一次", file=sys.stderr)
            continue
        return first


async def _ensure_admin_password() -> None:
    """serve 启动前的引导：保证库里有一份可用的管理员密码。

    库里已经有就什么都不做；没有则有 tty 就交互式问，没有 tty 就生成随机密码
    落到 600 文件（systemd 场景）。
    """

    async def action(db: Database, auth: AuthService) -> None:
        if await auth.password_configured():
            return

        if sys.stdin.isatty():
            print("\n还没有管理员密码，现在设置（只需一次）：", file=sys.stderr)
            await auth.set_password(settings.admin_username, _prompt_new_password())
            print(
                "✓ 已写入数据库。以后改密码：robot-monitor-server passwd\n", file=sys.stderr
            )
            return

        # systemd 之类没有终端：随机生成，落文件而不是打进日志
        password = _generate_password()
        await auth.set_password(settings.admin_username, password)
        path = settings.data_dir / "initial-password"
        path.write_text(password + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        print(
            f"已生成初始管理员密码，写在 {path}\n"
            "登录一次后该文件会自动删除；也可以 `robot-monitor-server passwd` 立即改掉。",
            file=sys.stderr,
        )

    await _with_db(action)


async def _passwd() -> None:
    async def action(db: Database, auth: AuthService) -> None:
        if not await auth.password_configured():
            print("还没有设置过密码，现在设置：", file=sys.stderr)
            await auth.set_password(settings.admin_username, _prompt_new_password())
            print("✓ 已写入数据库")
            return

        current = getpass.getpass("当前密码: ")
        new_password = _prompt_new_password()
        if not await auth.change_password(current, new_password):
            print("当前密码不正确", file=sys.stderr)
            sys.exit(1)

        auth.clear_initial_password_file()
        print("✓ 已更新，所有已登录会话已失效")

    await _with_db(action)


# --- 设备注册审批 -------------------------------------------------------


async def _pending() -> None:
    async def action(db: Database, auth: AuthService) -> None:
        rows = await auth.list_enroll_requests("pending")
        if not rows:
            print("没有待批准的注册申请")
            return

        print(f"{'ID':<12} {'HOSTNAME':<22} {'ROOT':<26} {'FROM':<16} TIME")
        for row in rows:
            created = datetime.fromtimestamp(row["created_at"]).strftime("%m-%d %H:%M")
            print(
                f"{row['id'][:10]:<12} {(row['hostname'] or '-'):<22} "
                f"{(row['root_path'] or '-'):<26} {(row['remote_ip'] or '-'):<16} {created}"
            )
        print("\n批准：robot-monitor-server approve <ID>    拒绝：... reject <ID>")

    await _with_db(action)


async def _set_enroll(pending_id: str, status: str) -> None:
    async def action(db: Database, auth: AuthService) -> None:
        rows = await auth.list_enroll_requests("pending")
        matches = [row for row in rows if row["id"].startswith(pending_id)]
        if not matches:
            print(f"没有匹配的待批准申请：{pending_id}", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"前缀 {pending_id} 匹配到多个申请，请输入更长的 ID", file=sys.stderr)
            sys.exit(1)

        full_id = str(matches[0]["id"])
        hostname = matches[0]["hostname"] or full_id[:10]
        if not await auth.set_enroll_status(full_id, status):
            print("这个申请已经不在待批准状态了", file=sys.stderr)
            sys.exit(1)

        if status == "approved":
            print(f"✓ 已批准 {hostname}，B 会在几秒内自动领到 token")
        else:
            print(f"已拒绝 {hostname}")

    await _with_db(action)


if __name__ == "__main__":
    main()
