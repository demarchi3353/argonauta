import os
import time

import pytest

from argonauta.web.terminal import (
    MISSING_PYWINPTY,
    TerminalError,
    TerminalManager,
    TerminalSupport,
    default_shell,
    reset_support_cache,
    terminal_support,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="pty backend")

if os.name == "posix":
    from argonauta.web.terminal import TerminalSession


def _read_until(session, needle: bytes, timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        chunk = session.read(0.1)
        if chunk:
            buf += chunk
            if needle in buf:
                return buf
        elif chunk == b"":
            break
    return buf


@posix_only
def test_spawn_runs_a_shell_in_the_repo(tmp_path):
    session = TerminalSession.spawn(tmp_path, cols=100, rows=30, shell="/bin/sh")
    try:
        assert session.alive() and session.cols == 100
        session.write(b"pwd; echo ok-$((1+1))\n")
        out = _read_until(session, b"ok-2")
        assert b"ok-2" in out and str(tmp_path.resolve()).encode() in out
        session.resize(120, 40)
        assert session.rows == 40
    finally:
        session.kill()
    assert not session.alive()
    assert session.read(0.1) == b""


@posix_only
def test_exit_is_detected(tmp_path):
    session = TerminalSession.spawn(tmp_path, shell="/bin/sh")
    session.write(b"exit\n")
    deadline = time.monotonic() + 5
    while session.alive() and time.monotonic() < deadline:
        session.read(0.1)
    assert not session.alive()
    session.kill()  # idempotent


@posix_only
def test_manager_caps_sessions_and_cleans_up(tmp_path):
    manager = TerminalManager(max_sessions=2)
    a = manager.create(tmp_path, 80, 24)
    b = manager.create(tmp_path, 80, 24)
    with pytest.raises(TerminalError, match="too many"):
        manager.create(tmp_path, 80, 24)
    assert {s.id for s in manager.list_for(tmp_path)} == {a.id, b.id}
    manager.remove(a.id)
    assert manager.get(a.id) is None and not a.alive()
    manager.shutdown()
    assert not b.alive() and manager.get(b.id) is None


@posix_only
def test_bad_shell_raises(tmp_path):
    with pytest.raises(TerminalError):
        TerminalSession.spawn(tmp_path, shell="/nonexistent/shell")


def test_support_pty_backend_is_always_available():
    reset_support_cache()
    assert terminal_support("pty") == TerminalSupport(True, "pty", None)


def test_support_conpty_reports_the_missing_package(monkeypatch):
    import builtins
    real = builtins.__import__
    def fake(name, *a, **k):
        if name == "winpty":
            raise ImportError("no winpty")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    reset_support_cache()
    assert terminal_support("conpty") == TerminalSupport(False, "conpty", MISSING_PYWINPTY)
    reset_support_cache()


def test_manager_refuses_when_backend_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("argonauta.web.terminal.terminal_support", lambda backend=None: TerminalSupport(False, "conpty", MISSING_PYWINPTY))
    with pytest.raises(TerminalError, match="pywinpty"):
        TerminalManager(backend="conpty").create(tmp_path, 80, 24)


def test_argonauta_shell_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGONAUTA_SHELL", str(tmp_path / "missing"))
    with pytest.raises(TerminalError, match="ARGONAUTA_SHELL"):
        default_shell()
    exe = tmp_path / "sh"; exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    monkeypatch.setenv("ARGONAUTA_SHELL", str(exe))
    assert default_shell() == str(exe)
