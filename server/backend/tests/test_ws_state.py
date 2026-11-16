"""`/ws/agent` 上的状态帧与能力协商（端到端，进程内 WS）。

单元测试能证明 `StateStore` 的行为，但证明不了"WS 收到 `state` 帧会不会真的送到
store"——那三行接线错了，单元测试照样全绿。这条用例从握手开始，走真实协议、
真实鉴权、真实数据库，最后从 HTTP 接口读回结果。

顺带钉住一条兼容性：`hello` 里没有 `capabilities` 的老节点必须能正常连上，
列表里显示空能力而不是报错（真实的老 agent 就是长这样）。
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient

from robot_monitor_server.auth import AuthService
from robot_monitor_server.config import Settings
from robot_monitor_server.db import Database
from robot_monitor_server.main import create_app
from robot_monitor_server.protocol import Message, MessageType

PASSWORD = "ws-state-123"


def bootstrap(
    tmp_path,
    *,
    capabilities: list[str] | None,
    profiles: list[str] | None,
    max_frame_kbps: object = None,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        storage_dir=tmp_path / "storage",
        serve_frontend=False,
    )
    settings.ensure_dirs()

    async def run() -> tuple[str, str]:
        db = Database(settings.db_path)
        await db.connect()
        try:
            auth = AuthService(db, settings)
            await auth.set_password("admin", PASSWORD)
            return await auth.issue_agent_token("b-state", hostname="state-test")
        finally:
            await db.close()

    agent_id, token = asyncio.run(run())
    app = create_app(settings)

    payload = {
        "hostname": "state-test",
        "os": "linux",
        "agent_version": "test",
        "roots": ["/srv/share"],
        "protocol": 1,
    }
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if profiles is not None:
        payload["media_profiles"] = profiles
    if max_frame_kbps is not None:
        payload["max_frame_kbps"] = max_frame_kbps
    hello = Message(type=MessageType.HELLO, agent_id=agent_id, payload=payload)
    return app, agent_id, token, hello


def state_frame(version: int, battery: int) -> str:
    return Message.state_frame(
        {
            "state_version": version,
            "observed_at": time.time(),
            "robot": {"mode": "idle", "battery": battery},
        }
    ).to_json()


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待超时（最后状态：{last!r}）")


def login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def first_agent(client: TestClient) -> dict | None:
    items = client.get("/api/v1/agents").json()["items"]
    return items[0] if items else None


def stored_version(store, agent_id: str, expected: int):
    record = asyncio.run(store.get(agent_id))
    return record if record is not None and record.state_version == expected else None


def metrics_with_state(client: TestClient) -> dict | None:
    body = client.get("/api/v1/system/metrics").json()
    return body if body["state"]["per_agent"] else None


def test_state_frames_reach_the_store_and_keep_the_newest(tmp_path):
    app, agent_id, token, hello = bootstrap(
        tmp_path, capabilities=["state.v1"], profiles=["low"]
    )

    with TestClient(app) as client:
        login(client)
        store = app.state.ctx.states

        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_text(hello.to_json())
            welcome = json.loads(ws.receive_text())
            assert welcome["type"] == "ack"

            ws.send_text(state_frame(1, battery=70))
            ws.send_text(state_frame(2, battery=72))
            # 乱序的旧帧：必须被丢弃，不能把界面刷回 battery=10
            ws.send_text(state_frame(1, battery=10))

            record = wait_for(lambda: stored_version(store, agent_id, 2))
            assert record.state["robot"]["battery"] == 72

            # 第三帧（版本 1）被处理过才会让这个计数 +1。
            wait_for(lambda: store.stale_dropped >= 1)

        # 能力落库：WS 已经断开（online=false），所以这里读到的是库里最后声明的值，
        # 正好验证"hello 写了库"而不是只在内存里漂着。
        listed = wait_for(lambda: first_agent(client))
        assert listed["online"] is False
        assert listed["capabilities"] == ["state.v1"]
        assert listed["media_profiles"] == ["low"]
        assert store.stale_dropped >= 1


def test_old_agent_without_capabilities_still_connects(tmp_path):
    """老 agent 的 hello 没有 capabilities 字段——不能因此握手失败。"""
    app, _agent_id, token, hello = bootstrap(tmp_path, capabilities=None, profiles=None)

    with TestClient(app) as client:
        login(client)

        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_text(hello.to_json())
            welcome = json.loads(ws.receive_text())
            assert welcome["type"] == "ack"

            listed = wait_for(lambda: first_agent(client))
            assert listed["capabilities"] == []
            assert listed["media_profiles"] == []


def test_metrics_expose_state_freshness_and_control_counters(tmp_path):
    """指标骨架：能算的算，没采集的必须是 null 而不是 0。"""
    app, _agent_id, token, hello = bootstrap(tmp_path, capabilities=[], profiles=[])

    with TestClient(app) as client:
        login(client)

        empty = client.get("/api/v1/system/metrics").json()
        assert empty["control"]["control_unknown_total"] == 0
        assert empty["state"]["per_agent"] == []
        # 媒体与事件都接通了：没有会话/没有上报时是空列表，而不是 null。
        # 页面据此显示"暂无会话"，而不是把 null 当 0 或当成"没有这个指标"。
        assert empty["media"]["sessions"] == []
        assert empty["events"]["per_agent"] == []

        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_text(hello.to_json())
            assert json.loads(ws.receive_text())["type"] == "ack"
            ws.send_text(state_frame(1, battery=80))

            filled = wait_for(lambda: metrics_with_state(client))

        entry = filled["state"]["per_agent"][0]
        assert entry["state_version"] == 1
        assert entry["freshness"] == "fresh"
        assert entry["state_freshness_ms"] >= 0


def test_hello_max_frame_kbps_is_parsed_and_sanitised(tmp_path):
    """能力协商里的自述上限（方案 §8.2）：合法值透出，非法值当"没声明"。

    不兜底的话，一个手写的 hello 就能把页面上的档位选择器撑成天文数字；
    而把非法值落成 0 又会让页面以为"这台机器一 kbps 都推不了"。
    """
    app, _agent_id, token, hello = bootstrap(
        tmp_path, capabilities=["media.ws.v1"], profiles=["low"], max_frame_kbps=2000
    )

    with TestClient(app) as client:
        login(client)
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_text(hello.to_json())
            assert json.loads(ws.receive_text())["type"] == "ack"

            listed = wait_for(lambda: first_agent(client))
            assert listed["max_frame_kbps"] == 2000


def test_hello_without_max_frame_kbps_reports_zero(tmp_path):
    app, _agent_id, token, hello = bootstrap(tmp_path, capabilities=[], profiles=[])

    with TestClient(app) as client:
        login(client)
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_text(hello.to_json())
            assert json.loads(ws.receive_text())["type"] == "ack"

            listed = wait_for(lambda: first_agent(client))
            assert listed["max_frame_kbps"] == 0


def test_ptz_with_bad_arguments_is_a_request_error_not_a_fake_success(tmp_path):
    """云台参数不合法必须是 400 + invalid_arguments。

    以前回 200 + `state=rejected`：命令日志里没有记录、页面拿不到错误码，
    排障时只能看到"点了没反应"。方案的命令状态机里 `rejected` 是明确终态，
    要有 code、要被审计。
    """
    app, agent_id, token, hello = bootstrap(tmp_path, capabilities=["camera.ptz"], profiles=["low"])

    with TestClient(app) as client:
        login(client)
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            ws.send_text(hello.to_json())
            assert json.loads(ws.receive_text())["type"] == "ack"

            # keepalive 不带方向：调用方漏传当前方向，明确拒绝（不能把球机停掉）
            response = client.post(
                f"/api/v1/agents/{agent_id}/camera/ptz",
                json={"action": "keepalive", "direct": "stop"},
            )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_arguments"
    assert "keepalive" in response.json()["message"]
