"""进程内滑动窗口限流。

只保护两个入口：登录（防爆破）和建任务（防误触发把磁盘塞满）。

不引第三方库：单进程、单用户，`deque` 够用。多 worker 时这套会失效，但
本系统本来就强制单 worker——这也是为什么这里敢用内存态。
"""

from __future__ import annotations

import time
from collections import deque


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, limit)
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True

    def retry_after(self, key: str) -> int:
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        return max(1, int(self.window - (time.monotonic() - bucket[0])))
