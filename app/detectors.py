"""座位观测器：从一帧画面得出"每个座位看到了什么"。

三种实现，接口一致，方便以后换：

  DiffSeatObserver  —— 零依赖。用"空镜参考帧"做逐座位像素比对。
                       不需要 GPU、不需要下载模型，今天就能跑。
                       缺点：对光照变化敏感，分不清"人"和"一堆书"。
  YoloSeatObserver  —— 装了 ultralytics 后自动可用。检测人体 + 脚点归属，
                       精度更高、更抗光照，是正式版的目标方案。
  MockObserver      —— 无相机时的演示数据。

设计要点：观测器只回答"这一帧"，不回答"这个座位算不算被占"。
后者是 state_machine 的事。分开之后，换模型不影响产品逻辑。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np

from . import config
from .seatmap import SeatMap, point_in_polygon
from .state_machine import Observation


class SeatObserver:
    name = "base"

    def observe(self, frame) -> dict[str, Observation]:
        raise NotImplementedError


# --------------------------------------------------------------------------
# 无模型：空镜参考帧 + 逐座位差分
# --------------------------------------------------------------------------
class DiffSeatObserver(SeatObserver):
    name = "diff"

    def __init__(self, seatmap: SeatMap, reference_bgr: np.ndarray):
        self.seatmap = seatmap
        self.reference = reference_bgr.copy()
        self._shape: tuple[int, ...] | None = None
        self._masks: dict[str, tuple[np.ndarray, int]] = {}
        self.last_scores: dict[str, float] = {}
        self._build_masks(reference_bgr.shape)

    def reframe(self, seatmap: SeatMap, reference_bgr: np.ndarray) -> None:
        self.seatmap = seatmap
        self.reference = reference_bgr.copy()
        self._build_masks(reference_bgr.shape)

    # -- 内部 --
    @staticmethod
    def _prep(frame: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        return gray.astype(np.int16)

    def _build_masks(self, shape) -> None:
        h, w = shape[:2]
        self._shape = tuple(shape)
        self._masks = {}
        for seat in self.seatmap.seats:
            pts = np.array([[int(p[0] * w), int(p[1] * h)] for p in seat.polygon], np.int32)
            m = np.zeros((h, w), np.uint8)
            cv2.fillPoly(m, [pts], 255)

            # 只统计座位"核心区"。相邻座位挨得近时，邻座的人体会溢出到本框边缘，
            # 不腐蚀的话会出现"空座位因为旁边有人而被判成暂离"。
            bw = int(pts[:, 0].max() - pts[:, 0].min())
            bh = int(pts[:, 1].max() - pts[:, 1].min())
            k = max(3, int(min(bw, bh) * 0.14) | 1)
            m = cv2.erode(m, np.ones((k, k), np.uint8))

            area = int(m.sum() // 255)
            if area < 50:
                continue
            self._masks[seat.id] = (m > 0, area)

    def observe(self, frame: np.ndarray) -> dict[str, Observation]:
        if frame is None:
            return {}
        if tuple(frame.shape) != self._shape:
            # 分辨率变了：参考帧等比缩放，掩码重建
            self.reference = cv2.resize(
                self.reference, (frame.shape[1], frame.shape[0])
            )
            self._build_masks(frame.shape)

        cur = self._prep(frame)
        ref = self._prep(self.reference)

        diff = cur - ref
        # 全局亮度补偿：窗户进光/关灯会让整幅图偏亮偏暗，先扣掉整体偏移
        offset = float(np.median(diff))
        delta = np.abs(diff - offset).astype(np.uint8)
        changed = (delta > config.DIFF_THRESHOLD).astype(np.uint8)
        # 去掉孤立噪点（相机噪点、飞虫）
        changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

        out: dict[str, Observation] = {}
        for sid, (mask, area) in self._masks.items():
            ratio = float(changed[mask].sum()) / max(1, area)
            self.last_scores[sid] = round(ratio, 3)

            if ratio >= config.PERSON_RATIO:
                conf = min(0.98, 0.55 + (ratio - config.PERSON_RATIO) / 0.5)
                out[sid] = Observation.person(ratio, conf)
            elif ratio >= config.BELONGING_RATIO:
                conf = min(0.85, 0.45 + (ratio - config.BELONGING_RATIO) / 0.35)
                out[sid] = Observation.belongings(ratio, conf)
            else:
                out[sid] = Observation.none(ratio, 0.85)
        return out


# --------------------------------------------------------------------------
# 有模型：YOLO 人体检测（装了 ultralytics 自动启用）
# --------------------------------------------------------------------------
class YoloSeatObserver(SeatObserver):
    name = "yolo"

    def __init__(self, seatmap: SeatMap, weights: str = "yolo11n.pt", conf: float = 0.35):
        from ultralytics import YOLO  # 延迟导入：没装也不影响其他模式

        self.seatmap = seatmap
        self.model = YOLO(weights)
        self.conf = conf
        self.last_scores: dict[str, float] = {}

    def reframe(self, seatmap: SeatMap, reference_bgr=None) -> None:
        self.seatmap = seatmap

    def observe(self, frame: np.ndarray) -> dict[str, Observation]:
        if frame is None:
            return {}
        h, w = frame.shape[:2]
        result = self.model.predict(frame, classes=[0], conf=self.conf, verbose=False)[0]

        counts: dict[str, int] = {s.id: 0 for s in self.seatmap.seats}
        confs: dict[str, float] = {s.id: 0.0 for s in self.seatmap.seats}

        for box in result.boxes:
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
            c = float(box.conf[0])
            # 脚点：bbox 底边中点。坐着的人底边大致落在椅面/腿部
            fx, fy = (x1 + x2) / 2.0 / w, y2 / h
            for seat in self.seatmap.seats:
                if point_in_polygon(fx, fy, seat.polygon):
                    counts[seat.id] += 1
                    confs[seat.id] = max(confs[seat.id], c)
                    break

        out: dict[str, Observation] = {}
        for sid, n in counts.items():
            self.last_scores[sid] = float(n)
            if n >= 1:
                out[sid] = Observation.person(float(n), min(0.99, confs[sid] + 0.1))
            else:
                out[sid] = Observation.none(0.0, 0.9)
        return out


# --------------------------------------------------------------------------
# 演示：无相机时的合成数据
# --------------------------------------------------------------------------
class MockObserver(SeatObserver):
    name = "mock"

    def __init__(self, seatmap: SeatMap, seed: int = 7):
        self.seatmap = seatmap
        self.rng = random.Random(seed)
        now = time.time()
        self._next: dict[str, float] = {}
        self._occupied: dict[str, bool] = {}
        for seat in seatmap.seats:
            self._occupied[seat.id] = self.rng.random() < 0.45
            self._next[seat.id] = now + self.rng.uniform(20, 240)
        self.last_scores: dict[str, float] = {}

    def reframe(self, seatmap: SeatMap, reference_bgr=None) -> None:
        self.seatmap = seatmap
        for seat in seatmap.seats:
            self._occupied.setdefault(seat.id, False)
            self._next.setdefault(seat.id, time.time() + 60)

    def _arrival_rate(self, hour: int) -> float:
        if 8 <= hour < 12 or 14 <= hour < 18 or 19 <= hour < 22:
            return 0.55
        if 12 <= hour < 14:
            return 0.15
        return 0.05

    def observe(self, frame=None) -> dict[str, Observation]:
        now = time.time()
        rate = self._arrival_rate(datetime.fromtimestamp(now).hour)
        out: dict[str, Observation] = {}
        for seat in self.seatmap.seats:
            sid = seat.id
            if now >= self._next[sid]:
                if self._occupied[sid]:
                    leave = self.rng.random() < 0.45
                    self._occupied[sid] = not leave
                    self._next[sid] = now + self.rng.uniform(120, 1800)
                else:
                    arrive = self.rng.random() < rate
                    self._occupied[sid] = arrive
                    self._next[sid] = now + self.rng.uniform(60, 900)

            if self._occupied[sid]:
                out[sid] = Observation.person(1.0, 0.92)
            elif self.rng.random() < 0.04:
                out[sid] = Observation.belongings(0.3, 0.6)
            else:
                out[sid] = Observation.none()
            self.last_scores[sid] = 1.0 if self._occupied[sid] else 0.0
        return out


def build_observer(mode: str, seatmap: SeatMap, reference_bgr=None) -> SeatObserver | None:
    if mode == "mock":
        return MockObserver(seatmap)
    if mode == "yolo":
        return YoloSeatObserver(seatmap)
    if mode == "diff":
        if reference_bgr is None:
            return None  # 还没拍空镜
        return DiffSeatObserver(seatmap, reference_bgr)
    return None
