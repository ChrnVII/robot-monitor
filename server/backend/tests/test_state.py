"""设备状态 Twin：版本单调、新鲜度分级、被拒即丢。

这些断言直接对应方案 §6.2 的三条硬规则。它们不是"实现细节的测试"——
每一条失效都会让页面显示一个看似合理、实际错误的数字：

- 版本不单调 → 101 迟到把界面刷回旧值；
- 不区分采集/接收时间 → 弱网下把"刚收到"当成"刚采集"；
- 超时后回退到上一次的在线布尔值 → 画面早就冻了，页面还写着"直播中"。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from robot_monitor_server.events import EventBus
from robot_monitor_server.state import (
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    MemoryStateStore,
    StateRecord,
    unknown_view,
)
from robot_monitor_server.ws import _on_state


def frame(version: int, *, observed_at: float | None = None, battery: int = 76) -> dict:
    return {
        "state_version": version,
        "observed_at": time.time() if observed_at is None else observed_at,
        "robot": {"mode": "idle", "battery": battery},
    }


# --- 版本单调 -----------------------------------------------------------


async def test_accepts_increasing_versions():
    store = MemoryStateStore()

    assert await store.put("b-1", frame(1, battery=70)) is True
    assert await store.put("b-1", frame(2, battery=71)) is True

    record = await store.get("b-1")
    assert record is not None
    assert record.state_version == 2
    assert record.state["robot"]["battery"] == 71


async def test_drops_late_frames_with_older_version():
    """先 102 后 101：必须保持 102。这是真实会发生的乱序。"""
    store = MemoryStateStore()

    await store.put("b-1", frame(102, battery=80))
    assert await store.put("b-1", frame(101, battery=10)) is False

    record = await store.get("b-1")
    assert record is not None
    assert record.state_version == 102
    assert record.state["robot"]["battery"] == 80
    assert store.stale_dropped == 1


async def test_same_version_is_not_accepted_twice():
    """重连可能导致同一帧被发两次，第二次不该覆盖（可能是不同连接的不同内容）。"""
    store = MemoryStateStore()

    await store.put("b-1", frame(5, battery=50))
    assert await store.put("b-1", frame(5, battery=99)) is False
    assert (await store.get("b-1")).state["robot"]["battery"] == 50


async def test_versions_are_per_device():
    store = MemoryStateStore()

    assert await store.put("b-1", frame(10)) is True
    assert await store.put("b-2", frame(1)) is True

    assert (await store.get("b-1")).state_version == 10
    assert (await store.get("b-2")).state_version == 1


# --- 非法帧 -------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"observed_at": time.time()},                       # 缺版本
        {"state_version": "7", "observed_at": time.time()},  # 版本是字符串
        {"state_version": True, "observed_at": time.time()}, # 布尔不是整数
        {"state_version": 1},                                # 缺采集时间
        {"state_version": 1, "observed_at": "now"},          # 采集时间不是数字
    ],
)
async def test_rejects_frames_that_cannot_be_ordered(payload):
    """没有 version/observed_at 就无法判新旧，只能丢——丢一帧好过错一个值。"""
    store = MemoryStateStore()

    assert await store.put("b-1", payload) is False
    assert await store.get("b-1") is None
    assert store.rejected == 1


# --- 新鲜度 -------------------------------------------------------------


def test_freshness_thresholds():
    now = 1000.0
    fresh = StateRecord("b-1", 1, observed_at=now - 1.0, received_at=now - 0.9)
    stale = StateRecord("b-1", 2, observed_at=now - 10.0, received_at=now - 9.9)
    gone = StateRecord("b-1", 3, observed_at=now - 40.0, received_at=now - 39.9)

    assert fresh.freshness(now) == FRESHNESS_FRESH
    assert stale.freshness(now) == FRESHNESS_STALE
    assert gone.freshness(now) == FRESHNESS_UNKNOWN


def test_view_carries_server_computed_age():
    """`age_ms` 由服务端算：前端的时钟不可信，也不该在后端之外出现第二套算法。"""
    now = 1000.0
    record = StateRecord("b-1", 7, observed_at=now - 2.5, received_at=now - 2.4)

    view = record.view(now)

    assert view["age_ms"] == 2500
    assert view["freshness"] == FRESHNESS_FRESH
    assert view["received_at"] == pytest.approx(now - 2.4)
    assert view["state_version"] == 7


def test_unknown_view_does_not_pretend_anything():
    view = unknown_view("b-1")

    assert view["freshness"] == FRESHNESS_UNKNOWN
    assert view["state"] == {}
    assert view["state_version"] is None
    # 关键：不给 online 字段，页面无法退回"上次是好的"这个错误结论。
    assert "online" not in view


# --- WS 侧接线 ----------------------------------------------------------


async def test_on_state_publishes_only_when_accepted():
    """被拒的帧不该推进 SSE：它携带的是旧值，推过去只会让前端闪一下。"""
    store = MemoryStateStore()
    bus = EventBus()
    queue = bus.subscribe()
    ctx = SimpleNamespace(states=store, bus=bus)

    try:
        await _on_state(ctx, "b-1", frame(1, battery=70))
        await _on_state(ctx, "b-1", frame(2, battery=72))
        await _on_state(ctx, "b-1", frame(1, battery=10))   # 乱序的旧帧

        first = await asyncio.wait_for(queue.get(), 1.0)
        second = await asyncio.wait_for(queue.get(), 1.0)

        assert first["type"] == "state"
        assert first["data"]["state_version"] == 1
        assert second["data"]["state_version"] == 2
        # 第三条被丢弃，队列里不应该再有内容
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), 0.05)
    finally:
        bus.unsubscribe(queue)


async def test_on_state_swallows_malformed_frames():
    """坏帧不能把连接打掉：状态是尽力而为的，丢一帧比断一条连接划算。"""
    store = MemoryStateStore()
    bus = EventBus()
    ctx = SimpleNamespace(states=store, bus=bus)

    await _on_state(ctx, "b-1", {"robot": {"battery": 1}})

    assert await store.get("b-1") is None
    assert store.rejected == 1
