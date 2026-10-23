"""Loopback-only HTTP server for the web UI.

Static page + JSON API + WebSocket terminals. Every API call needs the
per-run bearer token, a loopback Host and a matching Origin.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from argonauta.logging_setup import get_logger
from argonauta.web import api
from argonauta.web.jobs import JobRunner
from argonauta.web.terminal import TerminalManager
from argonauta.workspaces import Registry

log = get_logger()
MAX_BODY = 1024 * 1024
DRAIN_LIMIT = 16 * MAX_BODY  # read and discard up to this much so the client sees the 413
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/patch.js": ("patch.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/vendor/xterm.js": ("vendor/xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm.css": ("vendor/xterm.css", "text/css; charset=utf-8"),
    "/vendor/addon-fit.js": ("vendor/addon-fit.js", "text/javascript; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
}
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'"


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class AppContext:
    registry: Registry
    jobs: JobRunner
    terminals: TerminalManager
    token: str
    static_dir: Path
    port: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts()}


class ArgonautaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], ctx: AppContext):
        super().__init__(address, RequestHandler)
        self.ctx = ctx
        ctx.port = self.server_address[1]


def create_server(ctx: AppContext, host: str = "127.0.0.1", port: int = 0) -> ArgonautaServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the UI only listens on 127.0.0.1 or localhost")
    return ArgonautaServer(("127.0.0.1", port), ctx)


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ArgonautaServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - never log requests
        return

    @property
    def ctx(self) -> AppContext:
        return self.server.ctx

    # ----- plumbing -----
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.ctx.allowed_hosts()

    def _origin_ok(self, require: bool = False) -> bool:
        """Reject a foreign Origin. Same-origin fetch() GETs send none, so an
        absent header is allowed for the API (cross-site browser requests
        always carry one); WebSocket upgrades must present it."""
        origin = self.headers.get("Origin")
        if origin is None:
            return not require
        return origin in self.ctx.allowed_origins()

    def _token_ok(self, token: str | None = None) -> bool:
        if token is None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            token = auth[7:].strip()
        return hmac.compare_digest(token.encode(), self.ctx.token.encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            if length <= DRAIN_LIMIT:
                self.rfile.read(length)
            else:
                self.close_connection = True
            raise api.ApiError(413, "request body too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise api.ApiError(400, "body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise api.ApiError(400, "body must be a JSON object")
        return body

    def _drain_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= DRAIN_LIMIT:
            self.rfile.read(length)
        elif length > DRAIN_LIMIT:
            self.close_connection = True

    # ----- routing -----
    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if not self._host_ok():
            self._drain_body()
            self._send_json(403, {"error": "forbidden host"})
            return
        if path.startswith("/ws/"):
            self._handle_websocket(path)
            return
        if path.startswith("/api/"):
            self._handle_api(method, path, parse_qs(parts.query))
            return
        if method == "GET":
            self._serve_static(path)
            return
        self._drain_body()
        self._send_json(404, {"error": "not found"})

    def _serve_static(self, path: str) -> None:
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._send_json(404, {"error": "not found"})
            return
        name, content_type = entry
        file_path = self.ctx.static_dir / name
        if not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _handle_api(self, method: str, path: str, query: dict) -> None:
        try:
            if not self._token_ok():
                self._drain_body()
                raise api.ApiError(401, "missing or invalid token")
            if not self._origin_ok():
                self._drain_body()
                raise api.ApiError(403, "forbidden origin")
            body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
            status, payload = api.dispatch(self.ctx, method, path, query, body)
            self._send_json(status, payload)
        except api.ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the page
            log.exception("api %s %s failed", method, path)
            self._send_json(500, {"error": f"internal error: {exc.__class__.__name__}"})

    def _handle_websocket(self, path: str) -> None:
        import re

        from argonauta.web import websocket as wsproto

        m = re.match(r"^/ws/terminals/([A-Za-z0-9_]{3,40})$", path)
        if not self._origin_ok(require=True):
            self._send_json(403, {"error": "forbidden origin"})
            return
        if m is None or self.headers.get("Upgrade", "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            self._send_json(404, {"error": "not found"})
            return
        session = self.ctx.terminals.get(m.group(1))
        if session is None:
            self._send_json(404, {"error": "terminal not found"})
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsproto.accept_key(self.headers["Sec-WebSocket-Key"]))
        self.end_headers()
        self.close_connection = True
        conn = wsproto.WebSocketConnection(self.rfile, self.wfile)
        try:
            first = conn.recv()
            try:
                auth = json.loads(first.payload.decode("utf-8")) if first.opcode == wsproto.OP_TEXT else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                auth = {}
            if auth.get("type") != "auth" or not isinstance(auth.get("token"), str) or not self._token_ok(auth["token"]):
                conn.close(1008)
                return
            session.resize(int(auth.get("cols") or session.cols), int(auth.get("rows") or session.rows))
        except (wsproto.ConnectionClosed, wsproto.ProtocolError, ValueError):
            conn.close(1002)
            return

        stop = threading.Event()

        def pump() -> None:
            try:
                while not stop.is_set():
                    data = session.read(0.1)
                    if data is None:
                        continue
                    if data == b"":
                        break
                    conn.send_binary(data)
            except wsproto.ConnectionClosed:
                pass
            finally:
                stop.set()
                try:
                    conn.close(1000)
                except Exception:  # noqa: BLE001
                    pass

        reader = threading.Thread(target=pump, name=f"pty-{session.id}", daemon=True)
        reader.start()
        try:
            while not stop.is_set():
                frame = conn.recv()
                if frame.opcode == wsproto.OP_BINARY:
                    session.write(frame.payload)
                elif frame.opcode == wsproto.OP_TEXT:
                    try:
                        msg = json.loads(frame.payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if msg.get("type") == "resize":
                        session.resize(int(msg.get("cols") or session.cols), int(msg.get("rows") or session.rows))
        except (wsproto.ConnectionClosed, wsproto.ProtocolError, ValueError, OSError):
            pass
        finally:
            stop.set()
            self.ctx.terminals.remove(session.id)
            reader.join(1)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")
