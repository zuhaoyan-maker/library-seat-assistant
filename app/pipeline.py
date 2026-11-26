"""取流、主循环，以及把各部分串起来的 App。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import config
from .detectors import SeatObserver, build_observer
from .history import HistoryStore
from .seatmap import SeatMap
from .state_machine import RoomState


def _is_url(s: str) -> bool:
    return s.lower().startswith(("rtsp://", "rtmp://", "http://", "https://"))


class FrameSource:
    """后台线程持续读帧，只保留最新一帧。

    推理比取流慢得多，所以"丢掉旧帧、只处理最新帧"是正确做法：
    不会越积越多，延迟也不会雪球式增长。

    三种来源统一处理：摄像头编号、RTSP 地址、本地视频文件。
    视频文件会按原速播放并循环——用来在买相机之前先用真实画面验证算法。
    """

    def __init__(self, source: int | str | None, width: int = 2560, height: int = 1440,
                 loop: bool = True):
        self.source = source
        self.width = width
        self.height = height
        self.loop = loop
        self.disabled = source is None
        self.is_file = (
            isinstance(source, str) and not _is_url(source) and Path(source).is_file()
        )
        self.fps = 15.0
        self.cap: cv2.VideoCapture | None = None
        self.status = "init"
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._lock = threading.Lock()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._last_read = 0.0

    def start(self) -> None:
        if self.disabled:
            self.status = "disabled"
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="capture")
        self._thread.start()

    def preopen(self) -> None:
        """必须在主线程调用一次。

        macOS 的摄像头授权申请要求主 run loop，在采集线程里打开会失败，
        严重时直接让进程段错误。这里先在主线程把设备打开，
        后台线程只是复用同一个句柄。
        """
        if self.disabled or self.is_file:
            return
        if not self._open():
            self.status = "offline"

    def _open(self) -> bool:
        try:
            cap = cv2.VideoCapture(self.source)
        except Exception:
            return False
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            return False

        if self.is_file:
            fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
            self.fps = max(1.0, min(60.0, float(fps)))
            self.cap = cap
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            print(f"[video] 载入 {Path(str(self.source)).name}  {w}x{h}  {self.fps:.0f}fps  循环播放")
            return True

        # 先要 MJPG：USB 2.0 带宽下，2K 分辨率只有 MJPEG 才跑得动，
        # 否则摄像头会静默降到 720p，而你不会收到任何提示。
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        except Exception:
            pass
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ok, probe = cap.read()
        if ok and probe is not None:
            self.height, self.width = probe.shape[:2]
            with self._lock:
                self._frame = probe
                self._ts = time.time()
        print(f"[camera] 已连接 {self.source} -> {self.width}x{self.height}")

        self.cap = cap
        return True

    def _release(self) -> None:
        cap = self.cap
        self.cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def stop(self) -> None:
        """停止采集。

        关键：绝对不能在这个方法里直接 release()。
        采集线程可能正卡在 cap.read() 里（AVFoundation 的 grabImageUntilDate），
        从别的线程释放底层委托对象会造成 use-after-free，macOS 上直接段错误。
        正确做法是置停止标志、等线程退出，由线程自己在 finally 里释放。
        """
        self._stop = True
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)
            if t.is_alive():
                print(f"[camera] 警告：采集线程未在 5 秒内退出，交由它自行释放")
                return
        self._release()

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            self._release()

    def _loop(self) -> None:
        while not self._stop:
            if self.cap is None:
                if not self._open():
                    with self._lock:
                        self.status = "offline"
                    time.sleep(3.0)
                    continue

            cap = self.cap
            if cap is None:
                continue

            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None

            # 视频放完了就从头再来，否则页面会慢慢变成"全部未知"
            if (not ok or frame is None) and self.is_file and self.loop:
                try:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, frame = cap.read()
                except Exception:
                    ok, frame = False, None

            if not ok or frame is None:
                with self._lock:
                    self.status = "offline"
                if not self.is_file:
                    self._release()
                time.sleep(1.0)
                continue

            with self._lock:
                self._frame = frame
                self._ts = time.time()
                self.status = "ok"

            if self.is_file:
                target = 1.0 / self.fps
                dt = time.time() - self._last_read
                if 0 < dt < target:
                    time.sleep(target - dt)
                self._last_read = time.time()

    def latest(self) -> tuple[float, np.ndarray | None]:
        with self._lock:
            return self._ts, self._frame

    def is_live(self, max_age: float = 5.0) -> bool:
        with self._lock:
            return self._frame is not None and (time.time() - self._ts) < max_age


class App:
    """全局运行状态：座位图、状态机、观测器、历史、缓存。"""

    def __init__(
        self,
        seatmap: SeatMap,
        mode: str = "diff",
        source: int | str = 0,
        admin_token: str | None = None,
    ):
        self.mode = mode
        self.admin_token = admin_token
        self.seatmap = seatmap
        self.room = RoomState(seatmap)
        self.source = FrameSource(source)
        # 演示/视频数据单独存，别污染真实房间的历史和预测基线
        self.history = HistoryStore(path=self._history_path())

        self.source = FrameSource(source)
        self.reference: np.ndarray | None = None
        self.observer: SeatObserver | None = None

        self._lock = threading.RLock()
        self._auto_reference = False
        self._snapshot: dict = self._blank_snapshot()
        self._advice_cache: tuple[float, dict] = (0.0, {})
        self._forecast_cache: tuple[float, dict] = (0.0, {})
        self.reports: list[dict] = []

        if mode != "mock":
            ref_path = config.REFERENCE_FILE
            if ref_path.exists():
                img = cv2.imread(str(ref_path))
                if img is not None:
                    self.reference = img
            self.source.preopen()  # 主线程触发摄像头授权，别放后台线程
            if self.source.status == "offline":
                # 首次打不开就关掉后台线程的授权申请：那个路径会从非主线程
                # 调 AVFoundation，在 macOS 上可能直接段错误。
                os.environ["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
                print("[camera] 摄像头没打开（没授权 / 被占用 / 没插）。")
                print("         要只用视频就加 --source none，接好摄像头后重启即可。")
            self.source.start()
            self.observer = build_observer(mode, seatmap, self.reference)
            # 视频文件：默认用第一帧当空镜（录制时应先给 10 秒空房间）
            self._auto_reference = (
                mode == "diff" and self.reference is None and self.source.is_file
            )
        else:
            self.observer = build_observer("mock", seatmap)

    # ---------- 状态 ----------
    def _history_path(self) -> Path:
        if self.source.is_file:
            return config.DATA_DIR / "history_demo.jsonl"
        return config.HISTORY_FILE

    def source_label(self) -> str:
        if self.mode == "mock":
            return "演示数据"
        if self.source.disabled:
            return "未接相机"
        s = self.source.source
        if isinstance(s, int):
            return f"摄像头 {s}"
        if self.source.is_file:
            return Path(str(s)).name
        return "网络视频流"

    def _blank_snapshot(self) -> dict:
        snap = self.room.snapshot()
        snap.update(
            {
                "mode": self.mode,
                "camera": "init",
                "observer": getattr(self.observer, "name", "-"),
                "has_reference": self.reference is not None,
                "source": self.source_label(),
            }
        )
        return snap

    def camera_status(self) -> str:
        if self.mode == "mock":
            return "mock"
        if self.source.disabled:
            return "disabled"
        if self.source.status == "offline":
            return "offline"
        if self.observer is None:
            return "no_reference"
        if not self.source.is_live(8.0):
            return "stalled"
        return "ok"

    # ---------- 主循环 ----------
    def tick(self) -> None:
        now = time.time()

        # 视频源：第一帧到手后自动当空镜，省得用户还得掐时间点按钮
        if self._auto_reference and self.mode == "diff":
            if self.source.is_live():
                if self.capture_reference(persist=False):
                    self._auto_reference = False
                    print("[auto] 已用视频第一帧作为空镜参考")

        cam = self.camera_status()

        if self.mode == "mock":
            obs = self.observer.observe(None) if self.observer else {}
        elif self.observer is None or cam in ("offline", "stalled"):
            obs = {}  # 全部传 None -> 状态机超时后降级为 unknown
        else:
            _, frame = self.source.latest()
            obs = self.observer.observe(frame) if frame is not None else {}

        self.room.apply(obs, now)
        snap = self.room.snapshot()
        snap.update(
            {
                "mode": self.mode,
                "camera": cam,
                "observer": getattr(self.observer, "name", "-"),
                "has_reference": self.reference is not None,
                "source": self.source_label(),
            }
        )
        self.history.add(snap, now)

        # 无论相机什么状态都要刷新快照：离线时座位会退化为 unknown，
        # 前端必须能看到"数据不可用"，而不是停留在几分钟前的"全空"。
        with self._lock:
            self._snapshot = snap

    # ---------- 对外查询 ----------
    def live(self) -> dict:
        with self._lock:
            snap = dict(self._snapshot)
        snap["advice"] = self.advice()
        return snap

    def advice(self) -> dict:
        now = time.time()
        ts, cached = self._advice_cache
        if now - ts < 8.0 and cached:
            return cached

        data = self.history.advice()
        cam = self.camera_status()
        if cam not in ("ok", "mock"):
            # 没有画面就没有结论。这条规则比任何预测都重要。
            reason = {
                "offline": "相机不在线",
                "stalled": "画面卡住了",
                "no_reference": "还没拍空镜，无法判断",
                "disabled": "没有接相机",
            }.get(cam, "数据不可用")
            data = {
                "level": "unknown",
                "text": f"{reason}，暂时看不了座位情况",
                "free": data.get("free"),
                "total": data.get("total"),
                "unknown": data.get("unknown", 0),
                "p_find": 0.0,
            }

        self._advice_cache = (now, data)
        return data

    def forecast(self, hours: int = 3) -> dict:
        now = time.time()
        ts, cached = self._forecast_cache
        if now - ts < 30.0 and cached:
            return cached
        data = self.history.forecast(hours)
        data["points_now"] = self._now_point()
        self._forecast_cache = (now, data)
        return data

    def _now_point(self) -> dict:
        rec = self.history.recent(5)
        if not rec:
            return {}
        last = rec[-1]
        return {"ts": last["ts"], "free": last["free"], "total": last["total"]}

    # ---------- 操作 ----------
    def switch_source(self, path: str) -> None:
        """把数据源切换到一段本地视频（网页上传后调用）。"""
        # 注意：stop() 必须在锁外等。采集线程置帧时要拿这把锁，
        # 持锁 join 会互相等到超时。
        old = self.source
        old.stop()
        with self._lock:
            new = FrameSource(path)
            self.source = new

            if self.mode == "mock":
                self.mode = "diff"
            self.room = RoomState(self.seatmap)
            self.history = HistoryStore(path=self._history_path())
            self.reference = None
            self.observer = None
            self._auto_reference = True
            self._advice_cache = (0.0, {})
            self._forecast_cache = (0.0, {})
            try:
                config.REFERENCE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            self._snapshot = self._blank_snapshot()
            new.start()
            print(f"[source] 已切换到视频：{path}")

    def set_seatmap(self, seatmap: SeatMap) -> None:
        with self._lock:
            self.seatmap = seatmap
            self.room.replace_seatmap(seatmap)
            if self.observer is not None:
                self.observer.reframe(seatmap, self.reference)
            self._snapshot = self.room.snapshot()
            self._snapshot.update(
                {
                    "mode": self.mode,
                    "camera": self.camera_status(),
                    "observer": getattr(self.observer, "name", "-"),
                    "has_reference": self.reference is not None,
                    "source": self.source_label(),
                }
            )

    def capture_reference(self, persist: bool = True) -> bool:
        """把当前帧存为"空镜"参考帧，并重建观测器。

        persist=True 会写到磁盘，下次启动直接复用；
        视频上传时用 persist=False，避免把视频的空镜留给真实的摄像头。
        """
        _, frame = self.source.latest()
        if frame is None:
            return False
        self.reference = frame.copy()
        if persist:
            try:
                config.REFERENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(config.REFERENCE_FILE), frame)
            except Exception:
                pass
        if self.mode == "diff":
            self.observer = build_observer("diff", self.seatmap, self.reference)
        return True

    def jpeg(self, which: str = "snapshot") -> bytes | None:
        if which == "reference":
            frame = self.reference
        else:
            _, frame = self.source.latest()
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return buf.tobytes() if ok else None

    def debug_scores(self) -> dict:
        obs = getattr(self.observer, "last_scores", {}) or {}
        _, frame = self.source.latest()
        frame_info = (
            {"w": int(frame.shape[1]), "h": int(frame.shape[0])} if frame is not None else None
        )
        return {
            "mode": self.mode,
            "observer": getattr(self.observer, "name", "-"),
            "camera": self.camera_status(),
            "source": self.source_label(),
            "frame": frame_info,
            "thresholds": {
                "person": config.PERSON_RATIO,
                "belonging": config.BELONGING_RATIO,
                "diff": config.DIFF_THRESHOLD,
            },
            "scores": obs,
        }


class Pipeline(threading.Thread):
    def __init__(self, app: App):
        super().__init__(daemon=True, name="pipeline")
        self.app = app
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                self.app.tick()
            except Exception as exc:  # 单帧失败不能拖垮整个服务
                print(f"[pipeline] tick error: {exc!r}")
            time.sleep(config.TICK_SEC)

    def stop(self) -> None:
        self._stop = True
