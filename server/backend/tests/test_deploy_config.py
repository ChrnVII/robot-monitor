"""反代配置的静态体检。

`nginx -t` 只能在目标机上跑，而这里最需要防的恰恰是"本地一切正常、上线握手失败"
——新增一条 WebSocket 路径时忘了加 `Upgrade` 头，本机直连 uvicorn 永远复现不出来。
所以把"每个 /ws/ 的 location 都必须升级连接"变成一条会在 CI 里红的断言。

真正上线前仍然要在目标机跑 `nginx -t` 并用 websocat 验 101（方案 §10.2 验收项），
这个测试只是把"忘记改配置"这类低级但致命的错挡在前面。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CONF = (
    Path(__file__).resolve().parents[3] / "deploy" / "nginx" / "robot-monitor.conf"
)

pytestmark = pytest.mark.skipif(not CONF.is_file(), reason=f"没有反代配置：{CONF}")


def locations() -> dict[str, str]:
    """把 `location <path> { ... }` 解析成 路径 → 块内容。

    只处理这一份配置里出现的形态（没有嵌套 location），够用且不用引入解析器。
    """
    text = CONF.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for match in re.finditer(r"location\s+([=^~]*\s*[^\s{]+)\s*\{", text):
        # `location = /ws/agent` 里的 `=` 是匹配修饰符，不是路径的一部分。
        path = match.group(1).split()[-1]
        index = match.end()
        depth = 1
        while index < len(text) and depth:
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        blocks[path] = text[match.end() : index - 1]
    return blocks


@pytest.fixture(scope="module")
def blocks() -> dict[str, str]:
    return locations()


def test_expected_paths_are_all_present(blocks):
    """这几条路径少一条都有具体后果，列出来是为了让删改时先看到它们。"""
    assert "/ws/agent" in blocks                      # 控制 + 状态
    assert "/ws/agent/media" in blocks                # 媒体上行（C1）
    assert any(path.startswith("/ws/live/") for path in blocks)   # 观众下行
    assert "/internal/" in blocks                     # 事件批量 + 分片 + 快照
    assert "/api/events" in blocks                    # SSE


def test_every_websocket_location_upgrades(blocks):
    """WebSocket 路径必须显式升级且超时够长，否则握手 400 / 每分钟被掐一次。"""
    websocket_paths = {path: body for path, body in blocks.items() if "/ws/" in path}
    assert websocket_paths, "配置里没有任何 WebSocket 路径"

    for path, body in websocket_paths.items():
        assert "Upgrade $http_upgrade" in body, f"{path} 缺少 Upgrade 头"
        assert 'Connection "upgrade"' in body, f"{path} 缺少 Connection: upgrade"
        assert re.search(r"proxy_read_timeout\s+\d+", body), f"{path} 没有显式读超时"
        assert re.search(r"proxy_read_timeout\s+(\d+)", body).group(1) != "300", (
            f"{path} 用的是全局 300s 超时；空闲的 WS 会被代理层掐断"
        )


def test_event_stream_is_not_buffered(blocks):
    body = blocks["/api/events"]
    assert "proxy_buffering off" in body
    assert "chunked_transfer_encoding off" in body


def test_internal_uploads_allow_large_bodies(blocks):
    """8MB 分片在默认 1MB 上限下会被 413 打回；事件批量也在这个前缀下。"""
    body = blocks["/internal/"]
    assert "client_max_body_size" in body
    assert "proxy_request_buffering off" in body


def test_media_locations_do_not_buffer(blocks):
    """媒体路径不能开代理缓冲：一攒批就意味着端到端延迟从 300ms 涨到几秒。"""
    for path in ("/ws/agent/media", "/ws/live/"):
        target = next((key for key in blocks if key.rstrip("/") == path.rstrip("/")), None)
        assert target is not None, f"缺少 {path}"
        assert "proxy_buffering off" in blocks[target], f"{path} 没有关掉代理缓冲"
