"""设备事件入库与查询。

契约是 **at-least-once + 幂等消费**：机器人可能重发（补传窗口、游标回退、
进程重启），云端按 `(agent_id, event_seq)` 唯一键去重，重复不报错、只计数。

`acked_seq` 的语义要说清楚，否则游标会一路错下去：**它是"这个序号之前的
我都收齐了"**。因为机器人按 seq 顺序批量发送，所以一批里最大的 seq 就满足
这个条件；不需要扫描缺号——扫描在断点补传时会把游标卡在第一个空洞上，
而那个空洞可能来自一台已经下线的旧进程。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .db import Database

logger = logging.getLogger("robot-monitor.events")

#: 单批上限。与 agent 侧的 BATCH_LIMIT 对齐，略有余量。
MAX_BATCH = 200


class EventStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def ingest(
        self,
        agent_id: str,
        batch: list[dict[str, Any]],
        cursor: int = 0,
    ) -> dict[str, Any]:
        """收一批事件，返回 {acked_seq, duplicates, received}。"""
        if not batch:
            return {"acked_seq": cursor, "duplicates": 0, "received": 0}

        now = time.time()
        duplicates = 0
        acked = cursor
        async with self.db.write_transaction() as conn:
            for item in batch[:MAX_BATCH]:
                seq = _as_int(item.get("event_seq"))
                if seq is None:
                    # 没有序号的记录无法去重也无法推进游标：整条跳过，
                    # 但会计入 received，让机器人看到"发了但没收"。
                    continue
                event_id = str(item.get("event_id") or f"{agent_id}-{seq}")
                payload = item.get("payload")
                payload_json = json.dumps(payload if payload is not None else {}, ensure_ascii=False)
                cursor_row = await conn.execute(
                    "INSERT INTO device_events (agent_id, event_seq, event_id, event_type,"
                    " occurred_at, received_at, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(agent_id, event_seq) DO NOTHING",
                    (
                        agent_id,
                        seq,
                        event_id,
                        str(item.get("event_type") or "unknown")[:64],
                        _as_float(item.get("occurred_at")) or now,
                        now,
                        payload_json,
                    ),
                )
                if cursor_row.rowcount == 0:
                    duplicates += 1
                acked = max(acked, seq)
        return {"acked_seq": acked, "duplicates": duplicates, "received": len(batch)}

    async def query(
        self,
        *,
        agent_id: str | None = None,
        cursor: int = 0,
        limit: int = 200,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """按游标拉取。游标是"我看到了 seq=N"，返回 > N 的记录。

        注意 **event_seq 是设备内序号**，不是全局序号。所以：
        - 带 `agent_id` 查询时是严格的时间线，游标语义正确；
        - 不带 `agent_id` 时多条设备的时间线会交错，此时按"收到时间"排序，
          游标只用来过滤，不能当成"全局进度"。
        """
        sql = (
            "SELECT agent_id, event_seq, event_id, event_type, occurred_at, received_at,"
            " payload_json FROM device_events WHERE event_seq > ?"
        )
        params: list[Any] = [cursor]
        if agent_id:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY event_seq LIMIT ?" if agent_id else " ORDER BY received_at LIMIT ?"
        params.append(max(1, min(limit, 500)))

        rows = await self.db.fetchall(sql, tuple(params))
        return [
            {
                "agent_id": row["agent_id"],
                "event_seq": int(row["event_seq"]),
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "occurred_at": float(row["occurred_at"]),
                "received_at": float(row["received_at"]),
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]

    async def stats(self, agent_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT COUNT(*) AS n, COALESCE(MAX(event_seq), 0) AS max_seq FROM device_events"
        params: tuple[Any, ...] = ()
        if agent_id:
            sql += " WHERE agent_id = ?"
            params = (agent_id,)
        row = await self.db.fetchone(sql, params)
        return {
            "count": int(row["n"]) if row else 0,
            "max_seq": int(row["max_seq"]) if row else 0,
        }


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
