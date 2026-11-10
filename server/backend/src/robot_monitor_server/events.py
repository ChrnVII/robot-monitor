"""SSE 事件总线。

给 C 的实时状态：节点上下线、任务状态变化、进度。

三个约束：

1. **发布绝不阻塞。** 一个卡住的浏览器不能拖住上传流程。队列满了就丢事件，
   客户端重连后靠 `GET /tasks` 对账补齐——终态事件本来就必须能被对账兜住。
2. **每个订阅者一个独立队列。** 共享队列会让一个慢客户端吃掉别人的事件。
3. **事件不可重放。** 不做 last_event_id 补发，因为补发需要把整个状态存一份，
   而 `GET /tasks` 已经是那份状态。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 256) -> None:
        self.maxsize = maxsize
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = {"type": event_type, "data": data, "ts": time.time()}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 慢客户端：丢这条，不丢连接、不阻塞发布方
                continue
