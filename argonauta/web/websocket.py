"""Just enough of RFC 6455 for one local browser tab talking to one pty.

Server side only: client frames must be masked, server frames are not.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import threading
from dataclasses import dataclass
from typing import BinaryIO

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1 << 20  # 1 MiB; terminal traffic is tiny
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10
_CONTROL = {OP_CLOSE, OP_PING, OP_PONG}


class ProtocolError(Exception):
    pass


class ConnectionClosed(Exception):
    pass


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class Frame:
    opcode: int
    payload: bytes
    fin: bool = True


def _encode(opcode: int, payload: bytes, mask: bool = False, fin: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        head += bytes([mask_bit | length])
    elif length < 65536:
        head += bytes([mask_bit | 126]) + struct.pack(">H", length)
    else:
        head += bytes([mask_bit | 127]) + struct.pack(">Q", length)
    if mask:
        key = os.urandom(4)
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return head + key + masked
    return head + payload


def encode_frame(opcode: int, payload: bytes, mask: bool = False) -> bytes:
    return _encode(opcode, payload, mask=mask, fin=True)


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise ConnectionClosed("peer closed the connection")
        buf += chunk
    return buf


def read_frame(stream: BinaryIO, require_mask: bool = True) -> Frame:
    b0, b1 = _read_exact(stream, 2)
    if b0 & 0x70:
        raise ProtocolError("reserved bits set")
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(stream, 8))[0]
    if length > MAX_FRAME:
        raise ProtocolError("frame too large")
    if opcode in _CONTROL and (length > 125 or not fin):
        raise ProtocolError("invalid control frame")
    if require_mask and not masked:
        raise ProtocolError("client frames must be masked")
    key = _read_exact(stream, 4) if masked else b""
    payload = _read_exact(stream, length)
    if masked:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return Frame(opcode=opcode, payload=payload, fin=fin)


class WebSocketConnection:
    def __init__(self, rfile: BinaryIO, wfile: BinaryIO):
        self._r = rfile
        self._w = wfile
        self._lock = threading.Lock()
        self.closed = False

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._lock:
            if self.closed:
                return
            try:
                self._w.write(encode_frame(opcode, payload))
                self._w.flush()
            except (OSError, ValueError):
                self.closed = True
                raise ConnectionClosed("write failed")

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send(OP_BINARY, data)

    def close(self, code: int = 1000) -> None:
        try:
            self._send(OP_CLOSE, struct.pack(">H", code))
        except ConnectionClosed:
            pass
        self.closed = True

    def recv(self) -> Frame:
        """Return the next TEXT or BINARY message, reassembling fragments."""
        message_op: int | None = None
        buffer = b""
        while True:
            frame = read_frame(self._r)
            if frame.opcode == OP_PING:
                self._send(OP_PONG, frame.payload)
                continue
            if frame.opcode == OP_PONG:
                continue
            if frame.opcode == OP_CLOSE:
                self.close()
                raise ConnectionClosed("peer sent close")
            if frame.opcode in (OP_TEXT, OP_BINARY):
                if message_op is not None:
                    raise ProtocolError("new message while fragment pending")
                if frame.fin:
                    return frame
                message_op, buffer = frame.opcode, frame.payload
                continue
            if frame.opcode == OP_CONT:
                if message_op is None:
                    raise ProtocolError("continuation without start")
                buffer += frame.payload
                if len(buffer) > MAX_FRAME:
                    raise ProtocolError("message too large")
                if frame.fin:
                    return Frame(opcode=message_op, payload=buffer, fin=True)
                continue
            raise ProtocolError(f"unknown opcode {frame.opcode}")
