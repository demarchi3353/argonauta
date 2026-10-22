import base64
import json
import os
import socket
import struct
import time

import pytest

from argonauta.web import websocket as ws
from tests.test_api import api  # noqa: F401 - fixture


class WsClient:
    def __init__(self, port, path, origin=None, host=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        host = host or f"127.0.0.1:{port}"
        origin = origin or f"http://127.0.0.1:{port}"
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        self.status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/1.1") else 0
        self.file = self.sock.makefile("rb")
        if self.status == 101:
            assert ws.accept_key(key).encode() in head

    def send_text(self, obj):
        self.sock.sendall(ws.encode_frame(ws.OP_TEXT, json.dumps(obj).encode(), mask=True))

    def send_binary(self, data):
        self.sock.sendall(ws.encode_frame(ws.OP_BINARY, data, mask=True))

    def recv(self, timeout=10):
        self.sock.settimeout(timeout)
        return ws.read_frame(self.file, require_mask=False)

    def read_until(self, needle: bytes, timeout=10):
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.recv(timeout=max(0.1, deadline - time.monotonic()))
            if frame.opcode == ws.OP_CLOSE:
                break
            buf += frame.payload
            if needle in buf:
                return buf
        return buf

    def close(self):
        try:
            self.sock.sendall(ws.encode_frame(ws.OP_CLOSE, struct.pack(">H", 1000), mask=True))
        except OSError:
            pass
        self.sock.close()


def _new_terminal(api):
    status, data = api.post(f"/api/repos/{api.repo.id}/terminals", {"cols": 80, "rows": 24})
    assert status == 201
    return data["terminal"]["id"]


def test_terminal_roundtrip_over_websocket(api):
    tid = _new_terminal(api)
    client = WsClient(api.port, f"/ws/terminals/{tid}")
    assert client.status == 101
    client.send_text({"type": "auth", "token": api.token, "cols": 100, "rows": 30})
    client.send_binary(b"echo ws-$((2+2))\n")
    out = client.read_until(b"ws-4")
    assert b"ws-4" in out
    client.send_text({"type": "resize", "cols": 120, "rows": 40})
    session = api.ctx.terminals.get(tid)
    time.sleep(0.2)
    assert session.cols == 120 and session.rows == 40
    client.close()


def test_terminal_shell_dies_when_socket_closes(api):
    tid = _new_terminal(api)
    client = WsClient(api.port, f"/ws/terminals/{tid}")
    client.send_text({"type": "auth", "token": api.token, "cols": 80, "rows": 24})
    client.send_binary(b"echo ready\n")
    client.read_until(b"ready", timeout=5)
    session = api.ctx.terminals.get(tid)
    assert session is not None and session.alive()
    client.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and api.ctx.terminals.get(tid) is not None:
        time.sleep(0.05)
    assert api.ctx.terminals.get(tid) is None and not session.alive()


def test_websocket_requires_token_and_origin(api):
    tid = _new_terminal(api)
    bad = WsClient(api.port, f"/ws/terminals/{tid}", origin="http://evil.example")
    assert bad.status == 403
    client = WsClient(api.port, f"/ws/terminals/{tid}")
    client.send_text({"type": "auth", "token": "wrong", "cols": 80, "rows": 24})
    frame = client.recv()
    assert frame.opcode == ws.OP_CLOSE and struct.unpack(">H", frame.payload[:2])[0] == 1008
    assert WsClient(api.port, "/ws/terminals/t_nope").status == 404
    assert api.ctx.terminals.get(tid) is not None  # unauthenticated attempts do not kill the shell
    api.request("DELETE", f"/api/terminals/{tid}")
