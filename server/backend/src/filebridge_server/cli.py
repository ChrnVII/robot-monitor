"""命令行入口。

日常只需要一条命令：

    filebridge-server serve        # 首次会引导设置管理员密码

其余命令都是"出了情况才用"：`passwd` 改密码、`pending` / `approve` 处理新设备、
`revoke-token` 吊销、`list-agents` 查看。

`issue-token` / `hash-password` 保留为手工通道（自动化脚本、老部署迁移），
默认流程不需要它们。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import secrets
import sys
from datetime import datetime
from typing import Any

from .auth import AuthService, hash_password
from .config import settings
from .db import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filebridge-server",
        description="FileBridge A 端服务：WebSocket 控制通道 + 文件库",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动服务（默认命令）")
    serve.add_argument("--host", default=None, help="监听地址，默认取 FILEBRIDGE_HOST")
    serve.add_argument("--port", type=int, default=None, help="端口，默认 8000")
    serve.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")

    hp = sub.add_parser("hash-password", help="生成 Argon2 密码哈希")
    hp.add_argument("--password", default=None, help="不传则交互式输入（推荐）")

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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        run_server(host=args.host, port=args.port, reload=args.reload)
    elif command == "hash-password":
        print_hash_password(args.password)
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
        "filebridge_server.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=reload,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


def print_hash_password(password: str | None) -> None:
    if password is None:
        first = getpass.getpass("管理员密码: ")
        second = getpass.getpass("再输一次: ")
        if first != second:
            print("两次输入不一致", file=sys.stderr)
            sys.exit(1)
        if len(first) < 8:
            print("密码至少 8 位", file=sys.stderr)
            sys.exit(1)
        password = first

    digest = hash_password(password)
    print(digest)
    print(
        "\n把它写进环境变量（注意单引号，避免 $ 被 shell 展开）：\n"
        f"  FILEBRIDGE_ADMIN_PASSWORD_HASH='{digest}'",
        file=sys.stderr,
    )


async def _with_db(action: Any) -> Any:
    settings.ensure_dirs()
    # 把实际用的库路径打出来。这只为防一个具体的坑：在 systemd 部署里跑 CLI 却
    # 忘了加载 /etc/filebridge/filebridge.env，配置会退化成相对路径，于是悄悄
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
            f"  filebridge-agent --server https://<你的域名> --token {token} --root /path/to/share",
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

    三种情况：
    - 库里已经有 → 什么都不做
    - env 里有哈希（老部署）→ 导入库，之后由库说了算
    - 都没有 → 有 tty 就交互式问；没有 tty 就生成随机密码落到 600 文件
    """

    async def action(db: Database, auth: AuthService) -> None:
        if await auth.adopt_env_password():
            print(
                "已把 FILEBRIDGE_ADMIN_PASSWORD_HASH 导入数据库；"
                "以后改密码用 `filebridge-server passwd`，不需要再动环境变量。",
                file=sys.stderr,
            )
            return

        if await auth.password_configured():
            return

        if sys.stdin.isatty():
            print("\n还没有管理员密码，现在设置（只需一次）：", file=sys.stderr)
            await auth.set_password(settings.admin_username, _prompt_new_password())
            print(
                "✓ 已写入数据库。以后改密码：filebridge-server passwd\n", file=sys.stderr
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
            "登录一次后该文件会自动删除；也可以 `filebridge-server passwd` 立即改掉。",
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
        print("\n批准：filebridge-server approve <ID>    拒绝：... reject <ID>")

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
