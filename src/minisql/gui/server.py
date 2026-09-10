"""标准库 HTTP 服务，仅允许本机浏览器的同源请求。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import secrets
import threading
import time
import webbrowser

from minisql.gui.session import Session

STATIC = Path(__file__).with_name("static")
MAX_BODY = 262144
IDLE_SECONDS = 30 * 60


class WorkbenchServer(ThreadingHTTPServer):
    def __init__(self, address, path):
        self.path = Path(path).resolve()
        self.token = secrets.token_urlsafe(32)
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        super().__init__(address, Handler)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"

    def service_actions(self):
        with self.sessions_lock:
            expired = [self.sessions.pop(key) for key, session in list(self.sessions.items())
                       if session.expired(time.monotonic(), IDLE_SECONDS)]
        for session in expired:
            try:
                session.close()
            except Exception:
                logging.exception("关闭空闲 GUI 会话失败")

    def server_close(self):
        super().server_close()
        for session in list(self.sessions.values()):
            try:
                session.close()
            except Exception:
                logging.exception("关闭 GUI 会话失败")
        self.sessions.clear()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def reply(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    MAX_DRAIN = MAX_BODY * 4  # 拒绝超大请求体时最多消费的量，防止无限读取。

    def drain_body(self, length: int) -> None:
        """消费未读请求体；超过上限的部分不读，由连接关闭兜底。"""
        try:
            remaining = min(max(length, 0), self.MAX_DRAIN)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except (TimeoutError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.valid_host():
            self.reply(403, {"error": "仅允许本机地址访问"})
            return
        if self.path == "/api/config":
            self.reply(200, {"token": self.server.token, "directory": str(self.server.path),
                             "idle_minutes": IDLE_SECONDS // 60})
            return
        files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                 "/style.css": ("style.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if self.path not in files:
            self.reply(404, {"error": "页面不存在"})
            return
        name, mime = files[self.path]
        self.reply(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")

    def do_POST(self):
        if (not self.valid_host() or self.headers.get("Origin") not in (None, self.server.origin)
                or self.headers.get("X-MiniSQL-Token") != self.server.token):
            # 消费请求体，避免 Windows 在带未读数据关闭 socket 时丢弃 403。
            try:
                length = int(self.headers.get("Content-Length", "0"))
                self.drain_body(length)
            except ValueError:
                pass
            self.reply(403, {"error": "请求来源无效，请重新打开工作台"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                # 回 400 前先消费请求体，否则 Windows 关闭带未读数据的 socket
                # 会发 RST，客户端可能在读到响应前被断连（偶发 10053）。
                self.drain_body(length)
                raise ValueError("请求超过限制，SQL 文件需小于 256 KB")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象")
            action = self.path.removeprefix("/api/")
            if self.path != "/api/" + action or action not in (
                    "session", "connect", "state", "execute", "disconnect", "close"):
                self.reply(404, {"error": "接口不存在"})
                return
            if action == "session":
                with self.server.sessions_lock:
                    if len(self.server.sessions) >= 16:
                        raise ValueError("最多同时打开 16 个页面，请先断开并关闭其他页面")
                    key = secrets.token_urlsafe(24)
                    session = Session(self.server.path)
                    self.server.sessions[key] = session
                self.reply(200, {"ok": True, "session": key, **session.state()})
                return
            key = body.get("session")
            if not isinstance(key, str):
                raise ValueError("缺少会话标识")
            with self.server.sessions_lock:
                session = self.server.sessions.get(key)
                if session is None:
                    self.reply(410, {"error": "会话已结束或空闲超时，未提交事务已回滚；请刷新页面"})
                    return
                if action == "close":
                    self.server.sessions.pop(key)
                    future = None
                else:
                    future = session.submit(action, body)
            if future is None:
                session.close()
                self.reply(200, {"ok": True})
            else:
                self.reply(200, future.result())
        except (ValueError, UnicodeError) as error:
            self.reply(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.exception("GUI 请求失败")
            self.reply(500, {"error": "服务执行失败，请断开并重新连接；已提交的数据仍保留"})


def main():
    parser = argparse.ArgumentParser(description="MiniSQL 本地图形工作台")
    parser.add_argument("--data-dir", type=Path, default=Path("data/gui"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = WorkbenchServer(("127.0.0.1", args.port), args.data_dir)
    except OSError as error:
        parser.exit(1, f"无法启动工作台：{error}\n可使用 --port 指定其他端口。\n")
    with server:
        print(f"MiniSQL 工作台：{server.origin}\n数据库目录：{server.path}\n按 Ctrl+C 关闭服务并回滚未提交事务。", flush=True)
        if not args.no_browser:
            webbrowser.open(server.origin)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
