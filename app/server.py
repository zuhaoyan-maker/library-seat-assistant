"""HTTP 服务：静态页面 + JSON API + SSE 实时推送。

只用标准库，不需要 Node / 打包工具。
选 SSE 而不是 WebSocket：这里只需要"服务器 -> 浏览器"单向推送，
SSE 是纯 HTTP，浏览器 EventSource 原生支持，代码量少一个数量级，断线自动重连。
"""

from __future__ import annotations

import json
import mimetypes
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .pipeline import App
from .seatmap import SeatMap, polygon_area

WEB_ROOT = config.WEB_DIR.resolve()


def _local_addresses() -> set[str]:
    """本机所有地址（含回环和局域网 IP）。

    用局域网地址打开页面的人明明就坐在这台机器前，却因为 client_address 不是
    127.0.0.1 而被当成"外部用户"，上传会莫名 403 —— 这个坑在试点现场很容易踩。
    """
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(str(info[4][0]))
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips


LOCAL_IPS = _local_addresses()
MAX_UPLOAD = 800 * 1024 * 1024  # 800 MB，够放一段十几分钟的手机录像
ALLOWED_EXT = {"mp4", "mov", "m4v", "avi", "mkv", "webm"}
_SAFE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")


def _safe_name(name: str) -> str:
    """只保留文件名本身，去掉路径和危险字符。"""
    name = Path(str(name)).name
    name = _SAFE.sub("_", name).strip("._") or "video"
    stem, dot, ext = name.rpartition(".")
    if not dot or ext.lower() not in ALLOWED_EXT:
        name = f"{name}.mp4"
    return name[:80]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: App  # 由 serve() 注入

    # ---------------- 基础工具 ----------------
    def log_message(self, fmt, *args) -> None:
        if "/events" in self.path:
            return
        print(f"[http] {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 4_000_000:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _is_admin(self) -> bool:
        """标定页 / 画面 / 调试接口只对管理员开放。

        默认允许本机访问（安全，因为画面绝不该公开）；
        需要从手机访问时用 --admin-token 并带 ?token=...
        """
        token = self.app.admin_token
        if token:
            q = parse_qs(urlparse(self.path).query)
            if q.get("token", [""])[0] == token:
                return True

        # 走隧道/反向代理时，client_address 一律是 127.0.0.1，
        # 不能因此把互联网上的访问者当成管理员（否则谁都能看画面、传文件）。
        # cloudflared / nginx / frp 都会带上这类头。
        proxy_headers = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded")
        if any(h in self.headers for h in proxy_headers):
            return bool(token) and q.get("token", [""])[0] == token

        return self.client_address[0] in LOCAL_IPS

    def _static(self, rel: str) -> bool:
        rel = rel.lstrip("/") or "index.html"
        p = (WEB_ROOT / rel).resolve()
        if WEB_ROOT not in p.parents and p != WEB_ROOT:
            return False
        if not p.is_file():
            return False
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)
        return True

    # ---------------- 路由 ----------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        q = parse_qs(url.query)
        app = self.app

        if path == "/events":
            return self._sse()

        if path == "/api/health":
            return self._json({"ok": True, "ts": time.time(), "mode": app.mode})

        if path == "/api/state":
            return self._json(app.live())

        if path == "/api/seatmap":
            data = app.seatmap.to_dict()
            data["is_admin"] = self._is_admin()
            return self._json(data)

        if path == "/api/history":
            minutes = _int_arg(q, "minutes", 60, 1, 20160)
            return self._json({"records": app.history.recent(minutes)})

        if path == "/api/forecast":
            hours = _int_arg(q, "hours", 3, 1, 12)
            return self._json(app.forecast(hours))

        if path == "/api/advice":
            return self._json(app.advice())

        if path == "/api/camera.jpg" or path == "/api/snapshot.jpg":
            if not self._is_admin():
                return self._err(403, "仅管理员可访问画面")
            data = app.jpeg("snapshot")
            if data is None:
                return self._err(503, "相机无画面")
            return self._send(200, data, "image/jpeg")

        if path == "/api/reference.jpg":
            if not self._is_admin():
                return self._err(403, "仅管理员可访问画面")
            data = app.jpeg("reference")
            if data is None:
                return self._err(404, "还没有空镜参考帧")
            return self._send(200, data, "image/jpeg")

        if path == "/api/debug/scores":
            if not self._is_admin():
                return self._err(403, "仅管理员可访问")
            return self._json(app.debug_scores())

        if path in ("/calibrate", "/calibrate.html"):
            if not self._is_admin():
                return self._err(403, "标定页仅本机可访问（或带 ?token=）")
            return self._send(200, (WEB_ROOT / "calibrate.html").read_bytes(),
                              "text/html; charset=utf-8")

        if path in ("/", "/index.html"):
            return self._send(200, (WEB_ROOT / "index.html").read_bytes(),
                              "text/html; charset=utf-8")

        if self._static(path):
            return

        self._err(404, "not found")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        path = url.path
        app = self.app

        if path == "/api/upload":
            return self._upload(url)

        if path == "/api/report":
            body = self._read_json()
            seat_id = str(body.get("seat_id", ""))
            says = str(body.get("says", ""))
            if says not in ("person", "empty"):
                return self._err(400, "says 必须是 person 或 empty")
            app.reports.append({"ts": time.time(), "seat_id": seat_id, "says": says})
            del app.reports[:-500]
            print(f"[report] {seat_id} -> {says}")
            return self._json({"ok": True})

        if path == "/api/seatmap":
            if not self._is_admin():
                return self._err(403, "仅管理员可修改座位图")
            body = self._read_json()
            try:
                sm = SeatMap.from_dict(body)
            except (KeyError, TypeError, ValueError) as exc:
                return self._err(400, f"座位图格式错误: {exc}")
            if not sm.seats:
                return self._err(400, "至少需要一个座位")
            ids = set()
            for s in sm.seats:
                if s.id in ids:
                    return self._err(400, f"座位编号重复: {s.id}")
                ids.add(s.id)
                if len(s.polygon) < 3 or polygon_area(s.polygon) < 0.0008:
                    return self._err(400, f"座位 {s.id} 的多边形太小或无效")
            sm.save(config.SEATMAP_FILE)
            app.set_seatmap(sm)
            print(f"[seatmap] 已更新：{len(sm.seats)} 个座位")
            return self._json({"ok": True, "seats": len(sm.seats)})

        if path == "/api/reference":
            if not self._is_admin():
                return self._err(403, "仅管理员可操作")
            if app.mode == "mock":
                return self._err(400, "mock 模式不需要空镜")
            ok = app.capture_reference()
            if not ok:
                return self._err(503, "相机无画面，无法拍空镜")
            print("[reference] 已保存空镜参考帧")
            return self._json({"ok": True})

        self._err(404, "not found")

    # ---------------- 视频上传 ----------------
    def _upload(self, url) -> None:
        if not self._is_admin():
            return self._err(403, "仅本机可上传视频")
        q = parse_qs(url.query)
        fname = _safe_name(q.get("name", ["video.mp4"])[0])
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total <= 0:
            return self._err(400, "没有收到文件内容")
        if total > MAX_UPLOAD:
            return self._err(413, f"文件太大，上限 {MAX_UPLOAD // (1024 * 1024)} MB")

        updir = config.DATA_DIR / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        dest = updir / fname
        got = 0
        try:
            with dest.open("wb") as f:
                while got < total:
                    chunk = self.rfile.read(min(1 << 20, total - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
        except OSError as exc:
            return self._err(500, f"写入失败：{exc}")

        if got != total:
            try:
                dest.unlink()
            except OSError:
                pass
            return self._err(400, "上传中断")

        print(f"[upload] {fname}  {got / 1024 / 1024:.1f} MB")
        try:
            self.app.switch_source(str(dest))
        except Exception as exc:
            return self._err(500, f"切换视频失败：{exc}")

        return self._json({"ok": True, "file": fname, "bytes": got})

    # ---------------- SSE ----------------
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        last = ""
        try:
            while True:
                payload = json.dumps(self.app.live(), ensure_ascii=False)
                if payload != last:
                    self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    last = payload
                else:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def _int_arg(q: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(q.get(name, [default])[0])
    except (ValueError, IndexError):
        return default
    return max(lo, min(hi, v))


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def serve(app: App, host: str = "0.0.0.0", port: int = 8000) -> None:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    print()
    print("  座位助手已启动")
    print(f"    本机:   http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"    同一 WiFi: http://{_lan_ip()}:{port}")
    print(f"    标定页: http://127.0.0.1:{port}/calibrate")
    print("    Ctrl+C 停止")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 已停止")
    finally:
        httpd.server_close()
