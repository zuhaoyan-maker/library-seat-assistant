"""全局配置与运行期参数。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SEAT_DATA_DIR", ROOT / "data"))
WEB_DIR = ROOT / "web"

DATA_DIR.mkdir(parents=True, exist_ok=True)

SEATMAP_FILE = DATA_DIR / "seatmap.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
REFERENCE_FILE = DATA_DIR / "reference.jpg"

ROOM_NAME = os.environ.get("SEAT_ROOM_NAME", "自习室")

# ---- 状态机参数（秒）----
# 连续多久看到人 -> 判定占用
ON_SEC = float(os.environ.get("SEAT_ON_SEC", 15))
# 连续多久看不到人 -> 判定空位
OFF_SEC = float(os.environ.get("SEAT_OFF_SEC", 180))
# 多久没有新观测 -> 判定数据不可用
STALE_SEC = float(os.environ.get("SEAT_STALE_SEC", 45))
# 检测到物品的记忆时长：超过这个时间没人，就从"暂离"转"空位"
AWAY_MEMORY_SEC = float(os.environ.get("SEAT_AWAY_MEMORY_SEC", 900))

# ---- 采集参数 ----
TICK_SEC = float(os.environ.get("SEAT_TICK_SEC", 2.0))
HISTORY_INTERVAL_SEC = 30.0

# ---- 无模型检测器的阈值（座位"核心区"内变化像素占比）----
# 画框越贴合椅面+桌前缘，这些值越稳定。现场用 /calibrate 的"实时判分"调。
PERSON_RATIO = float(os.environ.get("SEAT_PERSON_RATIO", 0.20))
BELONGING_RATIO = float(os.environ.get("SEAT_BELONGING_RATIO", 0.06))
DIFF_THRESHOLD = int(os.environ.get("SEAT_DIFF_THRESHOLD", 26))
