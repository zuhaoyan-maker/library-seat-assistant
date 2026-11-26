"""座位模型：多边形、默认布局、几何工具。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Seat:
    id: str
    label: str
    polygon: list[list[float]]  # 归一化坐标 [[x, y], ...]，与分辨率无关
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "polygon": [[round(x, 5), round(y, 5)] for x, y in self.polygon],
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Seat":
        return cls(
            id=str(d["id"]),
            label=str(d.get("label") or d["id"]),
            polygon=[[float(p[0]), float(p[1])] for p in d["polygon"]],
            tags=list(d.get("tags") or []),
        )


@dataclass
class SeatMap:
    room: str = "自习室"
    layout: dict = field(default_factory=lambda: {"cols": 5, "aisle_after": 2})
    updated: float = 0.0
    seats: list[Seat] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "room": self.room,
            "layout": self.layout,
            "updated": self.updated,
            "seats": [s.to_dict() for s in self.seats],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SeatMap":
        return cls(
            room=d.get("room", "自习室"),
            layout=d.get("layout") or {"cols": 5, "aisle_after": 2},
            updated=float(d.get("updated") or 0),
            seats=[Seat.from_dict(s) for s in d.get("seats", [])],
        )

    # ---------- 持久化 ----------
    def save(self, path: Path) -> None:
        self.updated = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "SeatMap | None":
        if not path.exists():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    # ---------- 查询 ----------
    def by_id(self) -> dict[str, Seat]:
        return {s.id: s for s in self.seats}

    def seat_at(self, x: float, y: float) -> Seat | None:
        """归一化坐标 (x, y) 落在哪个座位多边形内。"""
        for s in self.seats:
            if point_in_polygon(x, y, s.polygon):
                return s
        return None


def default_seatmap(n: int = 15, cols: int = 5, room: str = "自习室") -> SeatMap:
    """生成 n 个座位的规则布局，用于首次启动时先能跑起来。

    真实房间一定和这个不一样，所以请用 /calibrate 页面重新画。
    """
    rows = max(1, (n + cols - 1) // cols)
    margin = 0.055
    gap_x, gap_y = 0.018, 0.03
    aisle_after = min(2, cols - 1)

    usable_w = 1.0 - 2 * margin - (cols - 1) * gap_x - 0.03  # 过道额外留白
    usable_h = 1.0 - 2 * margin - (rows - 1) * gap_y
    cw = usable_w / cols
    ch = usable_h / rows

    seats: list[Seat] = []
    for r in range(rows):
        y0 = margin + r * (ch + gap_y)
        for c in range(cols):
            idx = r * cols + c
            if idx >= n:
                break
            x0 = margin + c * (cw + gap_x) + (0.03 if c > aisle_after else 0.0)
            seats.append(
                Seat(
                    id=f"A{idx + 1}",
                    label=f"A{idx + 1}",
                    polygon=[
                        [x0, y0],
                        [x0 + cw, y0],
                        [x0 + cw, y0 + ch],
                        [x0, y0 + ch],
                    ],
                    tags=["window"] if c == 0 else [],
                )
            )
    return SeatMap(
        room=room,
        layout={"cols": cols, "aisle_after": aisle_after},
        updated=time.time(),
        seats=seats,
    )


def room15_seatmap(room: str = "自习室") -> SeatMap:
    """按"7 个隔断位 + 两张四人桌"生成座位图。

    对应 6.25m × 4m、高 2.53m 的小自习室：
        北侧长墙  7 个带侧板的隔断位
        中间      两张 2×2 四人桌
    这里给的是"正面看过去"的占位几何，真实位置仍要用 /calibrate 现场重画。
    """
    seats: list[Seat] = []
    groups: list[dict] = []

    # ---- 7 个隔断位：画面上方一条窄长带 ----
    mx, gap = 0.045, 0.011
    cw = (1.0 - 2 * mx - 6 * gap) / 7
    y0, y1 = 0.10, 0.31
    booth_ids: list[str] = []
    for i in range(7):
        x0 = mx + i * (cw + gap)
        sid = f"C{i + 1}"
        booth_ids.append(sid)
        seats.append(
            Seat(
                id=sid,
                label=str(i + 1),
                polygon=[[x0, y0], [x0 + cw, y0], [x0 + cw, y1], [x0, y1]],
            )
        )
    groups.append({"title": "隔断位", "cols": 7, "row": 0, "seat_ids": booth_ids})

    # ---- 两张四人桌：下方两个 2×2 方块 ----
    for gi, (name, bx0) in enumerate((("A 桌", 0.075), ("B 桌", 0.525))):
        bw, bh = 0.40, 0.50
        cgap, rgap = 0.022, 0.030
        cell_w = (bw - cgap) / 2
        cell_h = (bh - rgap) / 2
        ids: list[str] = []
        for r in range(2):
            for c in range(2):
                x0 = bx0 + c * (cell_w + cgap)
                yy0 = 0.43 + r * (cell_h + rgap)
                sid = f"{'A' if gi == 0 else 'B'}{r * 2 + c + 1}"
                ids.append(sid)
                seats.append(
                    Seat(
                        id=sid,
                        label=sid,
                        polygon=[
                            [x0, yy0],
                            [x0 + cell_w, yy0],
                            [x0 + cell_w, yy0 + cell_h],
                            [x0, yy0 + cell_h],
                        ],
                    )
                )
        groups.append({"title": name, "cols": 2, "row": 1, "seat_ids": ids})

    return SeatMap(
        room=room,
        layout={"groups": groups, "cols": 7, "aisle_after": 0},
        updated=time.time(),
        seats=seats,
    )


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """射线法。poly 为归一化坐标。"""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            denom = yj - yi
            if denom == 0:
                j = i
                continue
            if x < (xj - xi) * (y - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def polygon_area(poly: list[list[float]]) -> float:
    """鞋带公式，返回归一化面积（0~1）。"""
    if len(poly) < 3:
        return 0.0
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0
