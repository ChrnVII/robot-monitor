"""两条新 WebSocket 的握手分支。

不起数据库：这里只验证"没会话/没 token 就被关掉"，因为这两个分支一旦写错，
表现是"浏览器一直转圈"或"谁都能看画面"，都属于最不该靠人工发现的问题。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from robot_monitor_server.routes import live


class FakeSettings:
    cookie_name = "rm_session"


class FakeAuth:
    async def verify_session(self, raw: str | None) -> Any:
        return None

    async def authenticate_agent(self, header: str | None) -> Any:
        return None


class FakeCtx:
    settings = FakeSettings()
    auth = FakeAuth()
    media = None


def _client() -> TestClient:
    app = FastAPI()
    app.state.ctx = FakeCtx()
    app.include_router(live.router)
    return TestClient(app)


def test_viewer_without_session_is_rejected() -> None:
    with _client() as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/live/a1?cameraId=ptz"):
                pass
    assert error.value.code == 1008


def test_agent_media_without_token_is_rejected() -> None:
    with _client() as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/agent/media?cameraId=ptz&sessionId=s-1"):
                pass
    assert error.value.code == 4401
