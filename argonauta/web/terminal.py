"""Embedded terminal: backend selection and session management.

The platform-specific sessions live in terminal_posix (pty) and
terminal_windows (ConPTY via pywinpty); this module stays importable anywhere.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

MISSING_PYWINPTY = 'Terminals on Windows need the pywinpty package: pip install "argonauta[windows]"'


class TerminalError(RuntimeError):
    pass


@dataclass(frozen=True)
class TerminalSupport:
    available: bool
    backend: str
    reason: str | None


_support_cache: dict[str, TerminalSupport] = {}


def reset_support_cache() -> None:
    _support_cache.clear()


def terminal_support(backend: str | None = None) -> TerminalSupport:
    backend = backend or ("conpty" if os.name == "nt" else "pty")
    cached = _support_cache.get(backend)
    if cached is not None:
        return cached
    if backend == "conpty":
        try:
            import winpty  # noqa: F401
            result = TerminalSupport(True, "conpty", None)
        except ImportError:
            result = TerminalSupport(False, "conpty", MISSING_PYWINPTY)
    else:
        result = TerminalSupport(True, "pty", None)
    _support_cache[backend] = result
    return result


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def default_shell() -> str:
    override = os.environ.get("ARGONAUTA_SHELL", "").strip()
    if override:
        if not _is_executable(override):
            raise TerminalError(f"ARGONAUTA_SHELL is not an executable: {override}")
        return override
    if os.name != "nt":
        for path in (os.environ.get("SHELL", "").strip(), "/bin/zsh", "/bin/bash", "/bin/sh"):
            if _is_executable(path):
                return path
    else:
        for path in (shutil.which("pwsh.exe"), shutil.which("powershell.exe"), os.environ.get("COMSPEC"), shutil.which("cmd.exe")):
            if path and _is_executable(path):
                return path
    raise TerminalError("no usable shell found")


def spawn_session(backend: str, repo_root: Path, cols: int, rows: int, shell: str | None = None):
    if backend == "conpty":
        from argonauta.web import terminal_windows

        return terminal_windows.WindowsTerminalSession.spawn(repo_root, cols, rows, shell)
    from argonauta.web import terminal_posix

    return terminal_posix.TerminalSession.spawn(repo_root, cols, rows, shell)


class TerminalManager:
    def __init__(self, max_sessions: int = 20, backend: str | None = None):
        self._sessions: dict = {}
        self._lock = threading.Lock()
        self._max = max_sessions
        self.backend = backend

    @property
    def support(self) -> TerminalSupport:
        return terminal_support(self.backend)

    def create(self, repo_root: Path, cols: int, rows: int):
        sup = terminal_support(self.backend)
        if not sup.available:
            raise TerminalError(sup.reason)
        with self._lock:
            self._reap()
            if len(self._sessions) >= self._max:
                raise TerminalError("too many open terminals; close one first")
            session = spawn_session(sup.backend, repo_root, cols, rows)
            self._sessions[session.id] = session
            return session

    def _reap(self) -> None:
        for sid, session in list(self._sessions.items()):
            if not session.alive():
                session.kill()
                self._sessions.pop(sid, None)

    def get(self, session_id: str):
        with self._lock:
            return self._sessions.get(session_id)

    def list_for(self, repo_root: Path) -> list:
        with self._lock:
            return [s for s in self._sessions.values() if s.repo_root == repo_root and s.alive()]

    def remove(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.kill()

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.kill()


def __getattr__(name: str):
    # POSIX only: re-export TerminalSession lazily, since terminal_posix imports this module.
    if name == "TerminalSession" and os.name != "nt":
        from argonauta.web.terminal_posix import TerminalSession

        return TerminalSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
