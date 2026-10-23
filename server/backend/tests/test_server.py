"""服务端装配测试。

只验证"能起来、配置生效、健康检查可达"。业务逻辑的测试跟随各模块。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from filebridge_server.config import Settings
from filebridge_server.main import create_app
from filebridge_server.protocol import PROTOCOL_VERSION


def make_client(tmp_path) -> TestClient:
    cfg = Settings(data_dir=tmp_path / "data", storage_dir=tmp_path / "storage")
    return TestClient(create_app(cfg))


def test_health_ok(tmp_path):
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["protocol"] == PROTOCOL_VERSION
    assert body["storage_ready"] is True


def test_startup_creates_storage_layout(tmp_path):
    storage = tmp_path / "storage"
    with make_client(tmp_path):
        pass

    assert (storage / "staging").is_dir()
    assert (storage / "files").is_dir()
    assert (storage / "trash").is_dir()


def test_chunk_size_scales_with_file_size():
    cfg = Settings()
    small = cfg.chunk_size_for(10 * 1024 * 1024)          # 10MB
    large = cfg.chunk_size_for(5 * 1024**3)               # 5GB

    assert small == cfg.chunk_size_small
    assert large == cfg.chunk_size_large
    assert large > small
