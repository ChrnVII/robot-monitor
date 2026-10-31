"""端到端：真实 uvicorn + 真实 agent + 真实 HTTP 分片上传。

不 mock 任何一层。这条测试的价值在于它同时验证了：

- WS 握手、hello、指令配对
- 分片 PUT 的 Content-Length 校验与偏移写入
- 服务端重算 sha256 并原子落盘
- 任务状态机走到 done
- Range 下载、中文文件名、回收站

运行在独立线程里，所以它比单元测试慢（每次约 1–2 秒），但这类流程错一步
就是线上故障，值得。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

# 端到端测试要驱动一个真实的 agent，但它属于另一个项目：部署 A 时只有 server/
# 被拷过去，此时 agent 包并不存在。没装就整组跳过，而不是让 A 上的 pytest
# 在 import 阶段直接失败。
pytest.importorskip(
    "filebridge_agent",
    reason="端到端测试需要 agent 项目已装入本环境：在仓库根跑 `./dev.sh sync`",
)

from filebridge_agent.client import AgentClient
from filebridge_agent.config import AgentConfig
from filebridge_agent.sandbox import Sandbox

from filebridge_server.auth import AuthService, hash_password
from filebridge_server.config import Settings
from filebridge_server.db import Database
from filebridge_server.main import create_app

PASSWORD = "e2e-password-123"
CHUNK = 256 * 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, timeout: float, message: str):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"{message}（最后状态：{last!r}）")


@pytest.fixture()
def stack(tmp_path):
    share = tmp_path / "share"
    share.mkdir()

    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        admin_password_hash=hash_password(PASSWORD),
        serve_frontend=False,
        chunk_size_small=CHUNK,
        chunk_size_large=CHUNK,
        chunk_threshold=1,
        command_timeout_seconds=15.0,
        heartbeat_sweep_seconds=0.5,
        heartbeat_timeout_seconds=10.0,
    )
    settings.ensure_dirs()

    async def bootstrap_token() -> tuple[str, str]:
        db = Database(settings.db_path)
        await db.connect()
        try:
            return await AuthService(db, settings).issue_agent_token("b-test", hostname="e2e")
        finally:
            await db.close()

    agent_id, token = asyncio.run(bootstrap_token())

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    wait_for(lambda: server.started, 20, "uvicorn 没起来")

    base = f"http://127.0.0.1:{port}"
    agent_config = AgentConfig(
        server=base,
        token=token,
        root=str(share),
        heartbeat_interval=0.5,
        max_concurrent_uploads=3,
        open_timeout=5.0,
    )
    client = AgentClient(agent_config, Sandbox(str(share)))
    agent_loop = asyncio.new_event_loop()

    def run_agent() -> None:
        asyncio.set_event_loop(agent_loop)
        agent_loop.run_until_complete(client.run())

    agent_thread = threading.Thread(target=run_agent, daemon=True)
    agent_thread.start()

    http = httpx.Client(base_url=base, timeout=60.0)
    login = http.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
    assert login.status_code == 200, login.text

    wait_for(
        lambda: [
            item
            for item in http.get("/api/v1/agents").json()["items"]
            if item["id"] == agent_id and item["online"]
        ],
        20,
        "agent 没有上线",
    )

    try:
        yield SimpleNamespace(
            http=http,
            base=base,
            share=share,
            agent_id=agent_id,
            settings=settings,
        )
    finally:
        http.close()
        agent_loop.call_soon_threadsafe(client.stop)
        agent_thread.join(timeout=15)
        server.should_exit = True
        server_thread.join(timeout=15)
        try:
            agent_loop.close()
        except RuntimeError:
            pass


def create_task(stack, rel_path: str) -> dict:
    response = stack.http.post(
        "/api/v1/tasks", json={"agent_id": stack.agent_id, "path": rel_path}
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_task(stack, task_id: str, timeout: float = 30.0) -> dict:
    def poll():
        task = stack.http.get(f"/api/v1/tasks/{task_id}").json()
        return task if task["status"] in {"done", "failed", "canceled"} else None

    return wait_for(poll, timeout, f"任务 {task_id} 没有结束")


def test_fetch_file_end_to_end(stack):
    payload = bytes(range(256)) * 4096          # 1 MiB，4 个 256KB 块
    target = stack.share / "报告 2026.bin"
    target.write_bytes(payload)

    task = create_task(stack, "报告 2026.bin")
    finished = wait_task(stack, task["id"])

    assert finished["status"] == "done", finished
    assert finished["progress"] == 100.0
    assert finished["total_bytes"] == len(payload)

    files = stack.http.get("/api/v1/files").json()["items"]
    assert len(files) == 1
    entry = files[0]
    assert entry["name"] == "报告 2026.bin"
    assert entry["size"] == len(payload)
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()

    download = stack.http.get(f"/api/v1/files/{entry['id']}/download")
    assert download.status_code == 200
    assert download.content == payload
    # 哈希随文件一起下发：用户拿到文件就能 `sha256sum` 对照，不用去界面上抄
    assert download.headers["x-file-sha256"] == hashlib.sha256(payload).hexdigest()


def test_zero_byte_file(stack):
    (stack.share / "empty.txt").write_bytes(b"")

    task = create_task(stack, "empty.txt")
    finished = wait_task(stack, task["id"])

    assert finished["status"] == "done", finished
    entry = stack.http.get("/api/v1/files").json()["items"][0]
    assert entry["size"] == 0
    assert entry["sha256"] == hashlib.sha256(b"").hexdigest()


def test_range_download_returns_206(stack):
    payload = bytes(range(256)) * 4096
    (stack.share / "big.bin").write_bytes(payload)

    task = create_task(stack, "big.bin")
    assert wait_task(stack, task["id"])["status"] == "done"

    file_id = stack.http.get("/api/v1/files").json()["items"][0]["id"]
    response = stack.http.get(
        f"/api/v1/files/{file_id}/download", headers={"Range": "bytes=0-99"}
    )

    assert response.status_code == 206, response.status_code
    assert response.content == payload[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(payload)}"
    # 206 也带同一个值：它描述的是整个文件，不是这一百个字节
    assert response.headers["x-file-sha256"] == hashlib.sha256(payload).hexdigest()


def test_directory_and_traversal_are_rejected(stack, tmp_path):
    (stack.share / "sub").mkdir()

    response = stack.http.post(
        "/api/v1/tasks", json={"agent_id": stack.agent_id, "path": "sub"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "not_a_file"

    for bad in ("../secret.txt", "/etc/passwd", "sub/../../x"):
        response = stack.http.post(
            "/api/v1/tasks", json={"agent_id": stack.agent_id, "path": bad}
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_arguments"


def test_missing_file_reports_file_not_found(stack):
    response = stack.http.post(
        "/api/v1/tasks", json={"agent_id": stack.agent_id, "path": "nope.txt"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "file_not_found"


def test_trash_restore_and_purge(stack):
    (stack.share / "keep.txt").write_text("hello", encoding="utf-8")
    task = create_task(stack, "keep.txt")
    assert wait_task(stack, task["id"])["status"] == "done"

    file_id = stack.http.get("/api/v1/files").json()["items"][0]["id"]

    assert stack.http.delete(f"/api/v1/files/{file_id}").status_code == 200
    assert stack.http.get("/api/v1/files").json()["total"] == 0
    trashed = stack.http.get("/api/v1/files", params={"deleted": True}).json()["items"]
    assert len(trashed) == 1

    assert stack.http.post(f"/api/v1/files/{file_id}/restore").status_code == 200
    assert stack.http.get("/api/v1/files").json()["total"] == 1

    assert stack.http.delete(f"/api/v1/files/{file_id}").status_code == 200
    assert stack.http.delete(f"/api/v1/files/{file_id}/purge").status_code == 200
    assert stack.http.get("/api/v1/files", params={"deleted": True}).json()["total"] == 0


def test_cancel_running_task(stack):
    (stack.share / "huge.bin").write_bytes(b"\x00" * (8 * 1024 * 1024))

    task = create_task(stack, "huge.bin")
    stack.http.post(f"/api/v1/tasks/{task['id']}/cancel")

    finished = stack.http.get(f"/api/v1/tasks/{task['id']}").json()
    assert finished["status"] == "canceled"
    # 取消必须把 staging 清掉，否则用户以为取消了、磁盘还被占着
    assert list(stack.settings.staging_dir.glob("*.part")) == []


def test_unauthenticated_access_is_rejected(stack):
    anonymous = httpx.Client(base_url=stack.base, timeout=10.0)
    try:
        assert anonymous.get("/api/v1/files").status_code == 401
        assert anonymous.get("/api/v1/agents").status_code == 401
        assert anonymous.get("/api/v1/tasks").status_code == 401
        with anonymous.stream("GET", "/api/events") as response:
            assert response.status_code == 401
    finally:
        anonymous.close()


def test_sse_delivers_task_events(stack):
    (stack.share / "sse.txt").write_text("ping", encoding="utf-8")

    collected: list[dict] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            with stack.http.stream("GET", "/api/events", timeout=None) as response:
                for line in response.iter_lines():
                    if stop.is_set():
                        return
                    if line.startswith("data: "):
                        collected.append(json.loads(line[6:]))
        except Exception:  # noqa: BLE001 - 连接在 teardown 时被关掉是正常的
            return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.5)

    task = create_task(stack, "sse.txt")
    assert wait_task(stack, task["id"])["status"] == "done"

    wait_for(lambda: any(e["type"] == "task_update" for e in collected), 10, "没收到 SSE 事件")
    stop.set()

    types = {event["type"] for event in collected}
    assert "task_update" in types
    assert "task_progress" in types
    assert "file_added" in types


# --- 批量取件：目录展开 + 批量建任务 -------------------------------------


def preview(stack, paths: list[str]) -> dict:
    response = stack.http.post(
        "/api/v1/tasks/preview", json={"agent_id": stack.agent_id, "paths": paths}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_expands_directories_recursively(stack):
    """勾一个目录，A 要能把整棵树的文件数、总大小算准。"""
    docs = stack.share / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "a.md").write_bytes(b"a" * 10)
    (docs / "sub" / "b.md").write_bytes(b"b" * 20)
    (stack.share / "root.txt").write_bytes(b"c" * 30)

    plan = preview(stack, ["docs"])

    assert plan["file_count"] == 2
    assert plan["total_bytes"] == 30
    assert plan["truncated"] is False
    assert {item["rel"] for item in plan["files"]} == {"docs/a.md", "docs/sub/b.md"}
    # 目录本身不是任务
    assert all(item["name"] for item in plan["files"])


def test_preview_accepts_mixed_paths_without_duplicates(stack):
    """同时选目录和它里面的文件，不能算两遍。"""
    docs = stack.share / "docs"
    docs.mkdir()
    (docs / "a.md").write_bytes(b"x" * 5)

    plan = preview(stack, ["docs", "docs/a.md"])

    assert plan["file_count"] == 1
    assert plan["total_bytes"] == 5


def test_preview_reports_missing_path_as_skipped(stack):
    plan = preview(stack, ["nope.txt"])

    assert plan["file_count"] == 0
    assert plan["skipped_count"] == 1
    assert "不存在" in plan["skipped"][0]["reason"] or plan["skipped"][0]["rel"] == "nope.txt"


def test_preview_follows_symlink_to_file(stack):
    """软链接指向根目录内的文件：跟随它，并按目标大小计入。

    list_dir 给的是链接自身的长度，不是目标的——直接用会让合计偏小。
    """
    folder = stack.share / "links"
    folder.mkdir()
    (folder / "real.txt").write_bytes(b"y" * 9)
    try:
        (folder / "l.txt").symlink_to(folder / "real.txt")
    except (OSError, NotImplementedError):
        pytest.skip("这个文件系统建不了软链接")

    plan = preview(stack, ["links"])

    assert plan["file_count"] == 2
    assert plan["total_bytes"] == 18
    assert {item["rel"] for item in plan["files"]} == {"links/real.txt", "links/l.txt"}


def test_preview_skips_broken_symlink(stack):
    folder = stack.share / "broken"
    folder.mkdir()
    try:
        (folder / "dangling").symlink_to(folder / "missing.txt")
    except (OSError, NotImplementedError):
        pytest.skip("这个文件系统建不了软链接")

    plan = preview(stack, ["broken"])

    assert plan["file_count"] == 0
    assert plan["skipped_count"] == 1


def test_batch_fetch_creates_one_task_per_file(stack):
    docs = stack.share / "batch"
    (docs / "x").mkdir(parents=True)
    (docs / "one.txt").write_bytes(b"1" * 11)
    (docs / "x" / "two.txt").write_bytes(b"2" * 22)

    plan = preview(stack, ["batch"])
    assert plan["file_count"] == 2

    accepted = stack.http.post(
        "/api/v1/tasks/batch",
        json={"agent_id": stack.agent_id, "files": plan["files"]},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["accepted"] == 2

    # 后台建任务：等它们都出现在列表里并跑完
    def all_done():
        items = stack.http.get("/api/v1/tasks", params={"limit": 100}).json()["items"]
        batch = [item for item in items if item["path"].startswith("batch/")]
        if len(batch) < 2:
            return None
        return batch if all(item["status"] in {"done", "failed", "canceled"} for item in batch) else None

    batch = wait_for(all_done, 30, "批量任务没有全部结束")
    assert all(item["status"] == "done" for item in batch), batch

    files = stack.http.get("/api/v1/files").json()["items"]
    assert {item["rel_path"] for item in files} == {"batch/one.txt", "batch/x/two.txt"}


def test_preview_marks_truncated_when_over_the_limit(stack):
    """上限不是摆设：超了要明确告诉用户"只展开了一部分"。"""
    stack.settings.batch_max_files = 1
    docs = stack.share / "many"
    docs.mkdir()
    for index in range(3):
        (docs / f"f{index}.txt").write_bytes(b"x")

    plan = preview(stack, ["many"])

    assert plan["truncated"] is True
    assert plan["file_count"] == 1
