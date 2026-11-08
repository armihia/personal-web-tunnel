"""仅监听本机回环的映射管理 HTTP 服务。"""
from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import urllib.parse
import sys
sys.dont_write_bytecode = True

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "manager"

from .model import load_settings
from .service import TunnelService


class ManagerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, service: TunnelService):
        self.service = service
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(address, ManagerHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.host = f"127.0.0.1:{self.server_port}"


class ManagerHandler(BaseHTTPRequestHandler):
    server: ManagerServer

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def log_message(self, format, *args):
        pass

    def send_body(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, value: dict):
        self.send_body(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def check_request(self, mutation=False) -> bool:
        if self.headers.get("Host") != self.server.host:
            self.send_json(403, {"error": "管理界面只允许本机回环访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self.send_json(403, {"error": "拒绝跨站请求。"})
            return False
        if mutation and not secrets.compare_digest(self.headers.get("X-Manager-Token", ""), self.server.csrf):
            self.send_json(403, {"error": "管理会话已变化，请刷新页面。"})
            return False
        return True

    def do_GET(self):
        if not self.check_request():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            self.send_json(200, self.server.service.status())
        elif path == "/api/session":
            self.send_json(200, {"token": self.server.csrf})
        else:
            assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            if path not in assets:
                self.send_json(404, {"error": "页面不存在。"})
                return
            asset = Path(__file__).parent / "web" / assets[path]
            self.send_body(200, asset.read_bytes(), (mimetypes.guess_type(asset)[0] or "text/plain") + "; charset=utf-8")

    def mutate(self, method):
        if not self.check_request(mutation=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise ValueError("请求过大。")
            if length and self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("请求需要 JSON 格式。")
            value = json.loads(self.rfile.read(length)) if length else {}
            path = urllib.parse.urlsplit(self.path).path
            service = self.server.service
            if method == "POST" and path == "/api/mappings":
                service.mutate(method, value)
            elif method in {"PUT", "DELETE"} and path.startswith("/api/mappings/"):
                service.mutate(method, value, path.removeprefix("/api/mappings/"))
            elif method == "POST" and path == "/api/refresh":
                service.refresh_mappings()
            elif method == "POST" and path in {"/api/tunnel/start", "/api/tunnel/stop"}:
                service.set_connected(path.endswith("/start"))
            elif method == "POST" and path == "/api/shutdown":
                self.send_json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self.send_json(404, {"error": "操作不存在。"})
                return
            self.send_json(201 if method == "POST" and path == "/api/mappings" else 200, {"ok": True})
        except KeyError as exc:
            self.send_json(404, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        self.mutate("POST")

    def do_PUT(self):
        self.mutate("PUT")

    def do_DELETE(self):
        self.mutate("DELETE")


def main():
    parser = argparse.ArgumentParser(description="本机网站穿透管理器")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    settings = load_settings(root)
    service = TunnelService(root, (args.data_dir or root / "client").resolve(), settings, local_only=args.local_only)
    server = None
    try:
        server = ManagerServer(("127.0.0.1", settings["ManagerPort"]), service)
        service.start()
        print(f"管理界面：http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever(poll_interval=0.25)
    finally:
        if server:
            server.server_close()
        service.close()


if __name__ == "__main__":
    main()
