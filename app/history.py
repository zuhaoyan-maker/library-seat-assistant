"""历史记录、聚合与"什么时候来"的预测。

刻意不用复杂模型：图书馆的占用规律由作息和课表驱动，非常强。
"同星期几最近 N 周的中位数 + 当日偏差指数衰减" 在这个场景里足够好，
而且可解释、冷启动快、不会突然给出离谱结果。
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config

SLOT_MIN = 30  # 30 分钟一个桶
MIN_DAYS_FOR_FORECAST = 3


class HistoryStore:
    def __init__(self, path: Path | None = None, keep_days: int = 30):
        self.path = path or config.HISTORY_FILE
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self.records: list[dict] = []  # {ts, free, occupied, away, unknown, total}
        self._last_append = 0.0
        self._baseline_cache: tuple[float, dict] = (0.0, {})
        self._forecast_cache: tuple[float, dict] = (0.0, {})
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        cutoff = time.time() - self.keep_days * 86400
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("ts", 0) >= cutoff:
                        self.records.append(rec)
        except OSError:
            pass

    def add(self, snapshot: dict, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        s = snapshot["summary"]
        rec = {
            "ts": round(now, 1),
            "free": s["free"],
            "occupied": s["occupied"],
            "away": s["away"],
            "unknown": s["unknown"],
            "total": s["total"],
        }
        with self._lock:
            self.records.append(rec)
            if now - self._last_append < config.HISTORY_INTERVAL_SEC:
                return
            self._last_append = now
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def recent(self, minutes: int = 60) -> list[dict]:
        cutoff = time.time() - minutes * 60
        with self._lock:
            return [r for r in self.records if r["ts"] >= cutoff]

    # ---------- 预测 ----------
    def _baseline(self) -> dict[tuple[int, int], float]:
        """(weekday, slot) -> 可用座位数中位数。缓存 5 分钟，别每次重算全表。"""
        now = time.time()
        ts, cached = self._baseline_cache
        if cached and now - ts < 300:
            return cached

        buckets: dict[tuple[int, int], list[int]] = {}
        with self._lock:
            records = list(self.records)
        for r in records:
            dt = datetime.fromtimestamp(r["ts"])
            slot = (dt.hour * 60 + dt.minute) // SLOT_MIN
            key = (dt.weekday(), slot)
            buckets.setdefault(key, []).append(r["free"])

        baseline: dict[tuple[int, int], float] = {}
        for key, vals in buckets.items():
            vals.sort()
            baseline[key] = float(vals[len(vals) // 2])
        self._baseline_cache = (now, baseline)
        return baseline

    def _days_covered(self) -> int:
        with self._lock:
            if not self.records:
                return 0
            first = self.records[0]["ts"]
        return int((time.time() - first) / 86400) + 1

    def forecast(self, hours: int = 3) -> dict:
        """逐 30 分钟预测未来可用座位数 + 到馆找到座位的概率。"""
        now_c = time.time()
        ts_c, cached_c = self._forecast_cache
        if cached_c and now_c - ts_c < 25 and cached_c.get("hours") == hours:
            return cached_c

        baseline = self._baseline()
        now = time.time()
        days = self._days_covered()

        if days < MIN_DAYS_FOR_FORECAST or not baseline:
            res = {
                "status": "warmup",
                "days": days,
                "need_days": MIN_DAYS_FOR_FORECAST,
                "points": [],
                "hours": hours,
                "message": f"数据积累中（{days}/{MIN_DAYS_FOR_FORECAST} 天），暂不预测",
            }
            self._forecast_cache = (now_c, res)
            return res

        recent = self.recent(30)
        cur_free = recent[-1]["free"] if recent else None
        pred_now = self._predict_at(baseline, now)
        deviation = 0.0 if cur_free is None else cur_free - pred_now

        points = []
        steps = hours * 60 // SLOT_MIN
        for i in range(1, steps + 1):
            t = now + i * SLOT_MIN * 60
            base = self._predict_at(baseline, t)
            decay = math.exp(-(i * SLOT_MIN) / 90.0)  # 90 分钟衰减
            pred = max(0.0, base + deviation * decay)
            points.append(
                {
                    "ts": t,
                    "iso": datetime.fromtimestamp(t).strftime("%H:%M"),
                    "pred_free": round(pred, 1),
                    "p_find": round(1 - math.exp(-pred), 3),  # 泊松：至少有一个空位
                }
            )

        res = {
            "status": "ok",
            "days": days,
            "hours": hours,
            "deviation": round(deviation, 1),
            "points": points,
        }
        self._forecast_cache = (now_c, res)
        return res

    @staticmethod
    def _predict_at(baseline: dict[tuple[int, int], float], ts: float) -> float:
        dt = datetime.fromtimestamp(ts)
        key = (dt.weekday(), (dt.hour * 60 + dt.minute) // SLOT_MIN)
        if key in baseline:
            return baseline[key]
        # 回退：同一天相邻时段
        for off in (1, -1, 2, -2):
            alt = (key[0], key[1] + off)
            if alt in baseline:
                return baseline[alt]
        return 3.0  # 完全没有参照时的保守默认值

    def advice(self) -> dict:
        """一句话建议：现在来值不值。"""
        fc = self.forecast(3)
        recent = self.recent(5)
        cur = recent[-1] if recent else None
        if not cur:
            return {"level": "unknown", "text": "还没有数据"}

        free = cur["free"]
        total = cur["total"] or 1
        p_now = round(1 - math.exp(-free), 3)

        # 全部座位都看不到时，不能瞎给结论（尤其不能说"满座"）
        if cur.get("unknown", 0) >= total:
            return {
                "level": "unknown",
                "text": "暂时看不到座位情况",
                "free": free,
                "total": total,
                "unknown": cur.get("unknown", 0),
                "p_find": 0.0,
            }

        if free == 0:
            text, level = "现在满座，建议稍后", "warn"
        elif free / total >= 0.4:
            text, level = "现在空位充足，可以直接来", "good"
        else:
            text, level = "还有位置，但不多", "ok"

        if fc.get("status") == "ok" and fc["points"]:
            later = max(fc["points"], key=lambda p: p["pred_free"])
            if later["pred_free"] > free * 1.6 + 1:
                text += f"；{later['iso']} 后预计更空"
                level = "ok" if level == "good" else level

        return {
            "level": level,
            "text": text,
            "free": free,
            "total": total,
            "unknown": cur.get("unknown", 0),
            "p_find": p_now,
        }
