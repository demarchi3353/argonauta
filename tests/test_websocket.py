import io
import struct

import pytest

from argonauta.web import websocket as ws


def test_accept_key_matches_rfc_example():
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 5, 125, 126, 65535, 65536, 70000])
def test_frame_roundtrip_all_length_encodings(size):
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    data = ws.encode_frame(ws.OP_BINARY, payload, mask=True)
    frame = ws.read_frame(io.BytesIO(data))
    assert frame.opcode == ws.OP_BINARY and frame.fin and frame.payload == payload


def test_unmasked_client_frame_rejected():
    data = ws.encode_frame(ws.OP_TEXT, b"hi", mask=False)
    with pytest.raises(ws.ProtocolError, match="mask"):
        ws.read_frame(io.BytesIO(data))
    assert ws.read_frame(io.BytesIO(data), require_mask=False).payload == b"hi"


def test_oversized_frame_rejected():
    header = bytes([0x82, 0x80 | 127]) + struct.pack(">Q", ws.MAX_FRAME + 1) + b"\x00\x00\x00\x00"
    with pytest.raises(ws.ProtocolError, match="large"):
        ws.read_frame(io.BytesIO(header))


def test_truncated_stream_is_connection_closed():
    data = ws.encode_frame(ws.OP_BINARY, b"abcdef", mask=True)[:-3]
    with pytest.raises(ws.ConnectionClosed):
        ws.read_frame(io.BytesIO(data))


def test_connection_answers_ping_and_reassembles_fragments():
    client_stream = io.BytesIO(
        ws.encode_frame(ws.OP_PING, b"p", mask=True)
        + ws._encode(ws.OP_TEXT, b"hel", mask=True, fin=False)
        + ws._encode(ws.OP_CONT, b"lo", mask=True, fin=True)
        + ws.encode_frame(ws.OP_CLOSE, struct.pack(">H", 1000), mask=True)
    )
    out = io.BytesIO()
    conn = ws.WebSocketConnection(client_stream, out)
    frame = conn.recv()
    assert frame.opcode == ws.OP_TEXT and frame.payload == b"hello"
    with pytest.raises(ws.ConnectionClosed):
        conn.recv()
    written = out.getvalue()
    pong = ws.read_frame(io.BytesIO(written), require_mask=False)
    assert pong.opcode == ws.OP_PONG and pong.payload == b"p"
