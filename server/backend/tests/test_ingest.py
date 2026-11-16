"""C2 事件批量入口：入库之外，还要让带状态迁移的事件真的生效。

事件链路最容易做成"记账"：库里多了一行 `upload_failed`，而页面上那条上传
还在原地转圈。这条批测的就是后者——补传成功必须等于**状态也收敛了**。

（通道分工见方案 §3.3：控制/状态走 WSS C0，事件走 HTTP C2，大对象走 HTTP C3。）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robot_monitor_server.routes.ingest import apply_side_effects

pytestmark = pytest.mark.no_database


class FakeTasks:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.boom = boom

    async def handle_agent_event(self, agent_id: str, payload: dict) -> None:
        self.calls.append((agent_id, payload))
        if self.boom:
            raise RuntimeError("数据库暂时不可用")


def item(event_type: str, **payload) -> dict:
    return {"event_seq": 1, "event_id": "ev-1", "event_type": event_type, "payload": payload}


async def test_upload_failed_reaches_the_same_handler_as_the_control_channel():
    """C0 的 EVENT 帧与 C2 的补传必须走同一条处理路径，结论才能一致。"""
    tasks = FakeTasks()
    ctx = SimpleNamespace(tasks=tasks)

    await apply_side_effects(ctx, "b-1", [item("upload_failed", upload_id="u-1", code="timeout")])

    assert tasks.calls == [
        ("b-1", {"upload_id": "u-1", "code": "timeout", "kind": "upload_failed"})
    ]


async def test_events_without_state_transitions_are_ignored():
    """纯流水账事件（告警、任务进度）只入库，不触发云端状态变更。"""
    tasks = FakeTasks()
    ctx = SimpleNamespace(tasks=tasks)

    await apply_side_effects(
        ctx,
        "b-1",
        [item("alert.raised", code="person_intrusion"), item("event.gap", dropped=3)],
    )

    assert tasks.calls == []


async def test_broken_side_effect_does_not_fail_the_batch():
    """副作用失败不能把整批退回：事件已经入库，回放随时能再来一次。"""
    tasks = FakeTasks(boom=True)
    ctx = SimpleNamespace(tasks=tasks)

    await apply_side_effects(ctx, "b-1", [item("upload_failed", upload_id="u-1")])

    assert len(tasks.calls) == 1          # 试过一次，异常被吞掉


async def test_malformed_payload_is_skipped():
    tasks = FakeTasks()
    ctx = SimpleNamespace(tasks=tasks)

    await apply_side_effects(
        ctx,
        "b-1",
        [
            {"event_seq": 1, "event_type": "upload_failed", "payload": "not-a-dict"},
            {"event_seq": 2, "event_type": "upload_failed"},
        ],
    )

    assert tasks.calls == []
