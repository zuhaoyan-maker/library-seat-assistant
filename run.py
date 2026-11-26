#!/usr/bin/env python3
"""启动入口。

    python3 run.py --mock                      # 无相机演示
    python3 run.py                             # 用默认摄像头
    python3 run.py --source 1                  # 第二个摄像头
    python3 run.py --source rtsp://...         # IP 相机
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 重定向到日志文件时也保持实时输出，否则看不到启动信息和相机状态
sys.stdout.reconfigure(line_buffering=True)

from app import config  # noqa: E402
from app.pipeline import App, Pipeline  # noqa: E402
from app.seatmap import SeatMap, default_seatmap, room15_seatmap  # noqa: E402
from app.server import serve  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="图书馆自习位实时助手（原型）")
    p.add_argument("--source", default="0",
                   help="摄像头编号 / RTSP 地址 / 视频文件 / none（不接相机）")
    p.add_argument("--mock", action="store_true", help="不用相机，用合成数据演示")
    p.add_argument("--mode", default="diff", choices=["diff", "yolo"],
                   help="diff=无模型差分（默认）；yolo=装了 ultralytics 后可用")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--room", default=None, help="房间名")
    p.add_argument("--seats", type=int, default=15, help="默认座位数（仅首次生成用）")
    p.add_argument("--cols", type=int, default=5, help="默认布局列数（仅首次生成用）")
    p.add_argument("--layout", default="grid", choices=["grid", "room15"],
                   help="grid=规则网格；room15=7 个隔断位 + 两张四人桌")
    p.add_argument("--reset-seatmap", action="store_true", help="丢弃现有座位图重新生成")
    p.add_argument("--fast", action="store_true",
                   help="缩短状态机时序（4s/15s），用视频演示时能看到变化；真机不要用")
    p.add_argument("--admin-token", default=None,
                   help="允许远程访问标定页/画面的令牌")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.fast:
        # 演示视频里的人只坐几十秒，用真实时序（15s/180s）根本看不出变化
        config.ON_SEC = 4.0
        config.OFF_SEC = 15.0
        config.AWAY_MEMORY_SEC = 120.0
        print("[init] --fast：状态机时序缩短为 4s 判占用 / 15s 判空")

    seatmap = None if args.reset_seatmap else SeatMap.load(config.SEATMAP_FILE)
    if seatmap is None:
        room = args.room or config.ROOM_NAME
        if args.layout == "room15":
            seatmap = room15_seatmap(room)
        else:
            seatmap = default_seatmap(args.seats, args.cols, room)
        seatmap.save(config.SEATMAP_FILE)
        print(f"[init] 已生成默认座位图（{len(seatmap.seats)} 座）-> {config.SEATMAP_FILE}")
        print("[init] 默认布局只是占位，请打开 /calibrate 按真实房间重新画座位框")

    source: int | str | None = args.source
    if isinstance(source, str) and source.strip().lower() in ("none", "off", "no"):
        source = None  # 只用上传的视频，不碰摄像头
    elif isinstance(source, str) and source.isdigit():
        source = int(source)

    mode = "mock" if args.mock else args.mode
    try:
        app = App(seatmap, mode=mode, source=source, admin_token=args.admin_token)
    except ImportError as exc:
        print(f"[error] 启动 {mode} 模式失败：{exc}")
        print("        没装 ultralytics 时请用默认的 --mode diff，或加 --mock 演示。")
        return 1

    Pipeline(app).start()
    try:
        serve(app, args.host, args.port)
    finally:
        app.source.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
