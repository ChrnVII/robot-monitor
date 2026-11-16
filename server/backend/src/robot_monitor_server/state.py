"""设备状态 Twin：只留最新一版，版本必须单调。

三条规则（方案 §6.2），每一条都对应一类"页面在骗人"的事故：

1. **版本单调。** 机器人递增 `state_version`，云端只接受 `>` 当前版本。没有
   这条，101/102 乱序到达就会把界面刷回旧值，而且看不出错——数字都是"合法的"。
   全量快照与增量共用同一套版本，否则切换形态时会出现假的新鲜度。

2. **采集时间与接收时间分开。** `observed_at` 是机器人采集的时刻，
   `received_at` 是云端收到的时刻。两者之差就是页面要显示的"数据延迟"。
   只留一个时间戳的话，弱网下页面会把"刚收到"当成"刚采集"。

3. **新鲜度分级，且不回退。** fresh / stale / unknown 是三个不同的事实。
   超过 15s 只能显示"—"，**不能**退回到"最后一次见到的 online=true"——
   旧版远程监控最容易误导人的地方就是画面早就冻结了，页面还写着"直播中"。

存储形态：第一期是进程内字典（单 worker，见 `main.py` 的说明），每 30s 落一次
`agent_state` 表只为重启后有个初始值。接口做成 Protocol 是为了将来换 Redis
时业务代码一行不动。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

#: 采集时刻距今多久算"新鲜"。
FRESH_SECONDS = 3.0
#: 超过这个秒数就只能显示"—"，不再给出任何在线判断。
STALE_SECONDS = 15.0

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StateRecord:
    """一台设备的最新状态。"""

    agent_id: str
    state_version: int
    observed_at: float
    received_at: float
    state: dict[str, Any] = field(default_factory=dict)

    def freshness(self, now: float | None = None) -> str:
        age = (now if now is not None else time.time()) - self.observed_at
        if age < FRESH_SECONDS:
            return FRESHNESS_FRESH
        if age <= STALE_SECONDS:
            return FRESHNESS_STALE
        return FRESHNESS_UNKNOWN

    def view(self, now: float | None = None) -> dict[str, Any]:
        """给浏览器的形状。`age_ms` 由服务端算，前端不再自己减时钟。"""
        moment = now if now is not None else time.time()
        return {
            "agent_id": self.agent_id,
            "state_version": self.state_version,
            "observed_at": self.observed_at,
            "received_at": self.received_at,
            "age_ms": round(max(0.0, moment - self.observed_at) * 1000),
            "freshness": self.freshness(moment),
            "state": self.state,
        }


def unknown_view(agent_id: str) -> dict[str, Any]:
    """从未收到过状态。

    显式给出 `freshness=unknown`，而不是让前端把"没有数据"当"离线"或"在线"。
    """
    return {
        "agent_id": agent_id,
        "state_version": None,
        "observed_at": None,
        "received_at": None,
        "age_ms": None,
        "freshness": FRESHNESS_UNKNOWN,
        "state": {},
    }


class StateStore(Protocol):
    """状态存储的接口。改 Redis 时只换实现。"""

    async def put(self, agent_id: str, state: dict[str, Any]) -> bool: ...

    async def get(self, agent_id: str) -> StateRecord | None: ...

    async def snapshot(self) -> dict[str, StateRecord]: ...

    def forget(self, agent_id: str) -> None: ...

    def stats(self) -> dict[str, int]: ...


class MemoryStateStore:
    """进程内实现。单 worker 下足够，重启即丢（丢的是"最新状态"，会自己补上来）。"""

    def __init__(self) -> None:
        self._items: dict[str, StateRecord] = {}
        #: 版本落后被丢弃的次数。持续增长说明有乱序或重复连接，值得报警。
        self.stale_dropped = 0
        #: 缺少 state_version / observed_at 的帧数。这类帧无法比较新旧，只能拒绝。
        self.rejected = 0

    async def put(self, agent_id: str, state: dict[str, Any]) -> bool:
        version = state.get("state_version")
        observed_at = state.get("observed_at")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or not isinstance(observed_at, (int, float))
            or isinstance(observed_at, bool)
        ):
            # 没有版本就无法保证单调；宁可丢这一帧，也不能让界面错值。
            self.rejected += 1
            return False

        current = self._items.get(agent_id)
        if current is not None and version <= current.state_version:
            self.stale_dropped += 1
            return False

        self._items[agent_id] = StateRecord(
            agent_id=agent_id,
            state_version=version,
            observed_at=float(observed_at),
            received_at=time.time(),
            state=dict(state),
        )
        return True

    async def get(self, agent_id: str) -> StateRecord | None:
        return self._items.get(agent_id)

    def put_sync(
        self, agent_id: str, state: dict[str, Any], *, received_at: float | None = None
    ) -> bool:
        """重启恢复专用：直接覆盖，不比较版本。

        内存本来就是空的，没有"更新还是更旧"的问题；而 `received_at` 要用
        库里存的那个——用当前时间会让一帧十分钟前的状态看起来"刚收到"。
        新鲜度仍按 `observed_at` 算，所以恢复出来的旧状态会如实显示 stale。
        """
        version = state.get("state_version")
        observed_at = state.get("observed_at")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or not isinstance(observed_at, (int, float))
            or isinstance(observed_at, bool)
        ):
            return False
        self._items[agent_id] = StateRecord(
            agent_id=agent_id,
            state_version=version,
            observed_at=float(observed_at),
            received_at=received_at if received_at is not None else time.time(),
            state=dict(state),
        )
        return True

    async def snapshot(self) -> dict[str, StateRecord]:
        return dict(self._items)

    def forget(self, agent_id: str) -> None:
        """设备被注销时清掉，避免删了节点还留着一份状态。"""
        self._items.pop(agent_id, None)

    def stats(self) -> dict[str, int]:
        return {
            "agents": len(self._items),
            "stale_dropped": self.stale_dropped,
            "rejected": self.rejected,
        }
