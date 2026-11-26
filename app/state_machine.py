"""座位状态机：把"这一帧看到了什么"变成"这个座位算不算被占"。

产品体验的 80% 取决于这一层，不是模型。三条规则：
  1. 迟滞：人站起来拿书、接水、上厕所，不能立刻变空位。
  2. 不可用优先：没数据时宁可显示"未知"，也绝不显示"空"。
  3. 暂离是一等状态：东西在、人不在，不等于空位。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import config
from .seatmap import Seat, SeatMap

FREE = "free"
OCCUPIED = "occupied"
AWAY = "away"
UNKNOWN = "unknown"


@dataclass
class Observation:
    """单次观测结果。kind 决定证据类型。"""

    kind: str  # person | belongings | none
    score: float = 0.0  # 0~1，变化强度或检测置信度
    conf: float = 0.0

    @staticmethod
    def person(score: float = 1.0, conf: float = 0.9) -> "Observation":
        return Observation("person", score, conf)

    @staticmethod
    def belongings(score: float = 0.5, conf: float = 0.6) -> "Observation":
        return Observation("belongings", score, conf)

    @staticmethod
    def none(score: float = 0.0, conf: float = 0.8) -> "Observation":
        return Observation("none", score, conf)


class SeatRuntime:
    """单个座位的状态与各种计时器。"""

    def __init__(self, seat: Seat, now: float | None = None):
        now = now if now is not None else time.time()
        self.seat = seat
        self.state = UNKNOWN
        self.conf = 0.0
        self.since = now
        self.updated = now
        self.person_since: float | None = None
        self.clear_since: float | None = None
        self.last_belonging = 0.0

    def _set(self, state: str, conf: float, now: float) -> None:
        if state != self.state:
            self.state = state
            self.since = now
        self.conf = max(0.0, min(1.0, conf))
        self.updated = now

    def update(
        self,
        obs: Observation | None,
        now: float,
        on_sec: float | None = None,
        off_sec: float | None = None,
        stale_sec: float | None = None,
        away_memory: float | None = None,
    ) -> None:
        # 默认值在调用时读 config，而不是在函数定义时绑定 ——
        # 否则运行期就改不了这些参数（演示视频需要更短的时序）。
        on_sec = config.ON_SEC if on_sec is None else on_sec
        off_sec = config.OFF_SEC if off_sec is None else off_sec
        stale_sec = config.STALE_SEC if stale_sec is None else stale_sec
        away_memory = config.AWAY_MEMORY_SEC if away_memory is None else away_memory

        # 没有观测（相机断流 / 推理卡住）：超时就降级为未知
        if obs is None:
            if now - self.updated > stale_sec and self.state != UNKNOWN:
                self._set(UNKNOWN, 0.0, now)
            return

        self.updated = now

        # 首次观测（或从"未知"恢复）时直接采纳证据：
        # 迟滞是为了防止状态抖动，还没有状态时就没什么可防的。
        # 否则每次启动都要干等 3 分钟才出结果。
        if self.state == UNKNOWN:
            if obs.kind == "person":
                self.person_since = now
                self._set(OCCUPIED, obs.conf, now)
            elif obs.kind == "belongings":
                self.last_belonging = now
                self.clear_since = now
                self._set(AWAY, obs.conf * 0.8, now)
            else:
                self.clear_since = now
                self._set(FREE, obs.conf, now)
            return

        if obs.kind == "person":
            self.person_since = now if self.person_since is None else self.person_since
            self.clear_since = None
            if now - self.person_since >= on_sec:
                self._set(OCCUPIED, obs.conf, now)
            return

        # 没看到人
        self.person_since = None
        if self.clear_since is None:
            self.clear_since = now

        if obs.kind == "belongings":
            self.last_belonging = now

        clear_for = now - self.clear_since

        if clear_for < off_sec:
            # 还在宽限期内：保持原状态，但把置信度往下压一点
            self.conf = max(0.0, self.conf - 0.02)
            return

        if self.last_belonging and (now - self.last_belonging) < away_memory:
            self._set(AWAY, min(0.95, 0.5 + obs.conf * 0.3), now)
        else:
            self._set(FREE, obs.conf, now)

    def to_dict(self) -> dict:
        return {
            "id": self.seat.id,
            "label": self.seat.label,
            "tags": list(self.seat.tags),
            "polygon": [[round(x, 5), round(y, 5)] for x, y in self.seat.polygon],
            "state": self.state,
            "conf": round(self.conf, 2),
            "since": self.since,
            "updated": self.updated,
        }


class RoomState:
    """一间房的全部座位状态。"""

    def __init__(self, seatmap: SeatMap):
        self.seatmap = seatmap
        now = time.time()
        self.runtimes: dict[str, SeatRuntime] = {
            s.id: SeatRuntime(s, now) for s in seatmap.seats
        }

    def replace_seatmap(self, seatmap: SeatMap) -> None:
        self.seatmap = seatmap
        now = time.time()
        old = self.runtimes
        self.runtimes = {}
        for s in seatmap.seats:
            rt = old.get(s.id)
            if rt is None:
                rt = SeatRuntime(s, now)
            else:
                rt.seat = s  # 保留状态，但换成新的座位定义（标签/多边形可能变了）
            self.runtimes[s.id] = rt

    def apply(self, observations: dict[str, Observation], now: float) -> None:
        for sid, rt in self.runtimes.items():
            rt.update(observations.get(sid), now)

    def snapshot(self) -> dict:
        seats = [rt.to_dict() for rt in self.runtimes.values()]
        counts = {FREE: 0, OCCUPIED: 0, AWAY: 0, UNKNOWN: 0}
        for s in seats:
            counts[s["state"]] = counts.get(s["state"], 0) + 1
        return {
            "ts": time.time(),
            "room": self.seatmap.room,
            "layout": self.seatmap.layout,
            "seats": seats,
            "summary": {
                "total": len(seats),
                "free": counts[FREE],
                "occupied": counts[OCCUPIED],
                "away": counts[AWAY],
                "unknown": counts[UNKNOWN],
            },
        }
