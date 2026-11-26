#!/usr/bin/env python3
"""生成一段假的"自习室录像"，用来在没有相机的情况下跑通整条链路。

    python3 tools/make_demo_video.py            # -> data/demo.mp4

画面用的是和默认座位图完全一致的五列三行布局，所以直接上传就能对上，
不需要重新标定。开头 6 秒是空房间（给程序当空镜）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import random

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.seatmap import default_seatmap  # noqa: E402

W, H = 960, 540
FPS = 15
SECONDS = 90


def room_background(seatmap) -> np.ndarray:
    img = np.full((H, W, 3), (208, 214, 222), np.uint8)
    # 地板
    cv2.rectangle(img, (-10, int(H * 0.82)), (W + 10, H + 10), (176, 168, 156), -1)
    for seat in seatmap.seats:
        pts = np.array([[int(x * W), int(y * H)] for x, y in seat.polygon], np.int32)
        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        x1, y1 = pts[:, 0].max(), pts[:, 1].max()
        cv2.rectangle(img, (x0, y0), (x1, y1), (152, 142, 130), -1)      # 桌面
        cv2.rectangle(img, (x0, y0), (x1, y1), (108, 100, 92), 2)        # 边框
        cv2.rectangle(img, (x0 + 4, y1 - 10), (x1 - 4, y1), (120, 110, 100), -1)  # 桌前缘
    return img


def draw_person(img, seat, wave: float = 0.0) -> None:
    xs = [p[0] * W for p in seat.polygon]
    ys = [p[1] * H for p in seat.polygon]
    x0, x1, y0, y1 = int(min(xs)), int(max(xs)), int(min(ys)), int(max(ys))
    cx = (x0 + x1) // 2
    bw, bh = x1 - x0, y1 - y0
    # 身体：略微左右晃动，避免完全静止（真实的人会动）
    ox = int(np.sin(wave) * 2)
    cv2.ellipse(img, (cx + ox, y1 - 10), (int(bw * 0.37), int(bh * 0.34)),
                0, 0, 360, (58, 60, 86), -1)
    cv2.circle(img, (cx + ox, y0 + int(bh * 0.24)), int(bw * 0.12), (92, 88, 116), -1)


def draw_bag(img, seat) -> None:
    xs = [p[0] * W for p in seat.polygon]
    ys = [p[1] * H for p in seat.polygon]
    x0, x1, y0, y1 = int(min(xs)), int(max(xs)), int(min(ys)), int(max(ys))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2 + 8
    cv2.ellipse(img, (cx, cy), (int((x1 - x0) * 0.19), int((y1 - y0) * 0.15)),
                0, 0, 360, (46, 58, 94), -1)


def build_schedule(seatmap):
    """三批人陆续进来、坐下、离开。时长按真实情况给（25-40 秒），
    否则用默认 15 秒判定门槛永远看不到"占用"。"""
    rng = random.Random(3)
    events = []  # (start_sec, end_sec, seat_index, kind)
    idxs = list(range(len(seatmap.seats)))
    rng.shuffle(idxs)
    t = 8.0
    for turn in range(3):
        for k in range(3):
            i = idxs[(turn * 3 + k) % len(idxs)]
            start = t + k * 2.0
            dur = rng.uniform(25, 38)
            kind = "bag" if (k == 2 and turn % 2 == 1) else "person"
            events.append((start, start + dur, i, kind))
        t += 20.0
    return events


def main() -> int:
    seatmap = default_seatmap(15, 5)
    bg = room_background(seatmap)
    events = build_schedule(seatmap)

    out = Path(__file__).resolve().parent.parent / "data" / "demo.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    if not writer.isOpened():
        print("无法创建视频文件（编码器不可用）")
        return 1

    total = FPS * SECONDS
    for f in range(total):
        t = f / FPS
        img = bg.copy()
        for start, end, i, kind in events:
            if start <= t < end:
                if kind == "person":
                    draw_person(img, seatmap.seats[i], wave=t * 1.7 + i)
                else:
                    draw_bag(img, seatmap.seats[i])
        cv2.putText(img, f"t={t:05.1f}s", (12, 26), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (90, 90, 96), 2, cv2.LINE_AA)
        writer.write(img)

    writer.release()
    print(f"已生成 {out}  {W}x{H}  {FPS}fps  {SECONDS}s  {total} 帧")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
