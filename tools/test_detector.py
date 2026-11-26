#!/usr/bin/env python3
"""无相机自测：用合成画面验证差分观测器 + 状态机 + 预测。

    python3 tools/test_detector.py

这不是单元测试框架，就是几个断言，跑起来几秒钟。
在没有真实相机、没有 GPU 的环境里，它是唯一能证明"逻辑是对的"的东西。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.detectors import DiffSeatObserver, MockObserver  # noqa: E402
from app.history import HistoryStore  # noqa: E402
from app.seatmap import default_seatmap, point_in_polygon  # noqa: E402
from app.state_machine import FREE, OCCUPIED, RoomState  # noqa: E402

W, H = 960, 540
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        print(f"  \033[31m✗\033[0m {name}  {detail}")


def make_room() -> np.ndarray:
    """画一个假的阅览室：米色地面 + 15 个深色桌面。"""
    img = np.full((H, W, 3), (205, 214, 224), np.uint8)
    seatmap = default_seatmap(15, 5)
    for seat in seatmap.seats:
        pts = np.array([[int(x * W), int(y * H)] for x, y in seat.polygon], np.int32)
        cv2.rectangle(img, (pts[:, 0].min(), pts[:, 1].min()),
                      (pts[:, 0].max(), pts[:, 1].max()), (150, 140, 128), -1)
        cv2.rectangle(img, (pts[:, 0].min(), pts[:, 1].min()),
                      (pts[:, 0].max(), pts[:, 1].max()), (110, 100, 92), 2)
    return img


def draw_person(img: np.ndarray, seat, kind: str = "person", strength: float = 1.0) -> None:
    """在座位框里画一个人（或一个书包）。"""
    xs = [p[0] * W for p in seat.polygon]
    ys = [p[1] * H for p in seat.polygon]
    x0, x1 = int(min(xs)), int(max(xs))
    y0, y1 = int(min(ys)), int(max(ys))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    if kind == "person":
        # 上半身 + 头，占座位框约 45% 面积
        cv2.ellipse(img, (cx, y1 - 8), (int((x1 - x0) * 0.34), int((y1 - y0) * 0.30)),
                    0, 0, 360, (60, 62, 88), -1)
        cv2.circle(img, (cx, y0 + int((y1 - y0) * 0.28)), int((x1 - x0) * 0.11), (96, 92, 120), -1)
    else:
        # 书包：只占四分之一
        cv2.ellipse(img, (cx, cy + 6), (int((x1 - x0) * 0.17), int((y1 - y0) * 0.13)),
                    0, 0, 360, (48, 60, 96), -1)


def main() -> int:
    seatmap = default_seatmap(15, 5)
    empty = make_room()
    occupied = empty.copy()
    draw_person(occupied, seatmap.seats[2], "person")
    draw_person(occupied, seatmap.seats[7], "person")
    draw_person(occupied, seatmap.seats[11], "belongings")

    print("\n[1] 几何")
    seat = seatmap.seats[0]
    cx = sum(p[0] for p in seat.polygon) / 4
    cy = sum(p[1] for p in seat.polygon) / 4
    check("座位中心点落回自己", point_in_polygon(cx, cy, seat.polygon))
    check("相邻座位不误判", not point_in_polygon(cx, cy, seatmap.seats[1].polygon))
    check("座位外不算", not point_in_polygon(-0.1, -0.1, seat.polygon))

    print("\n[2] 差分观测器")
    obs = DiffSeatObserver(seatmap, empty)
    r_empty = obs.observe(empty)
    r_occ = obs.observe(occupied)
    check("空画面判为空", r_empty["A1"].kind == "none",
          f"A1={r_empty['A1'].kind} score={obs.last_scores.get('A1')}")
    check("有人座位判为人", r_occ["A3"].kind == "person",
          f"A3={r_occ['A3'].kind} score={obs.last_scores.get('A3')}")
    check("第二个有人座位也判对", r_occ["A8"].kind == "person",
          f"A8={r_occ['A8'].kind}")
    check("书包判为物品", r_occ["A12"].kind == "belongings",
          f"A12={r_occ['A12'].kind} score={obs.last_scores.get('A12')}")
    check("没人的座位仍为空", r_occ["A1"].kind == "none",
          f"A1={r_occ['A1'].kind} score={obs.last_scores.get('A1')}")

    print("\n[3] 光照变化鲁棒性")
    brighter = np.clip(empty.astype(np.int16) + 22, 0, 255).astype(np.uint8)
    r = obs.observe(brighter)
    n_free = sum(1 for v in r.values() if v.kind == "none")
    check("整幅变亮不造成误报", n_free == 15, f"误报 {15 - n_free} 个")

    print("\n[4] 状态机迟滞")
    room = RoomState(seatmap)
    now = time.time()
    # 有人：连续 20 秒后才判占用
    for i in range(11):
        room.apply(r_occ, now + i * 2)
    st = room.runtimes["A3"].state
    check("持续有人 -> 占用", st == OCCUPIED, f"state={st}")
    # 人刚走 1 分钟：不能立刻变空
    for i in range(30):
        room.apply(r_empty, now + 20 + i * 2)
    st = room.runtimes["A3"].state
    check("离开 1 分钟内不算空（迟滞生效）", st == OCCUPIED, f"state={st}")
    # 超过 3 分钟：判为空/暂离
    for i in range(80):
        room.apply(r_empty, now + 80 + i * 2)
    st = room.runtimes["A3"].state
    check("离开 3 分钟后转空", st in (FREE, "away"), f"state={st}")
    # 数据不可用
    for i in range(40):
        room.apply({}, now + 250 + i * 2)
    st = room.runtimes["A3"].state
    check("长时间无数据 -> 未知", st == "unknown", f"state={st}")

    print("\n[5] 预测")
    store = HistoryStore(path=Path("/tmp/_seat_test_hist.jsonl"))
    store.records.clear()
    base = time.time() - 4 * 86400
    for d in range(4):
        for slot in range(48):
            ts = base + d * 86400 + slot * 1800
            free = 3 if 10 <= slot // 2 <= 11 else 12
            store.records.append({"ts": ts, "free": free, "occupied": 15 - free,
                                  "away": 0, "unknown": 0, "total": 15})
    fc = store.forecast(3)
    check("有 4 天数据后能预测", fc["status"] == "ok", str(fc.get("status")))
    check("预测点数正确", len(fc.get("points", [])) == 6, str(len(fc.get("points", []))))
    check("概率在 0~1 之间",
          all(0 <= p["p_find"] <= 1 for p in fc.get("points", [])))

    print("\n[6] Mock 观测器")
    mock = MockObserver(seatmap, seed=1)
    res = mock.observe(None)
    check("覆盖全部座位", len(res) == 15, str(len(res)))

    print(f"\n{'=' * 46}")
    print(f"  通过 {PASS} / {PASS + FAIL}")
    print(f"{'=' * 46}\n")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
