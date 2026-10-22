"""Pty-backed shell sessions for the embedded terminal.

Each session is a real login shell running inside the repository. It inherits
the user's environment (so git, ssh and gpg behave exactly as in a terminal)
and dies when the WebSocket closes, when the manager removes it, or when the
server shuts down.
"""

from __future__ import annotations

import fcntl
import os
import pty
import secrets
import select
import signal
import struct
import termios
import threading
import time
from pathlib import Path

from argonauta.logging_setup import get_logger
from argonauta.models import utcnow

log = get_logger()
READ_CHUNK = 65536


class TerminalError(RuntimeError):
    pass


def default_shell() -> str:
    candidate = os.environ.get("SHELL", "").strip()
    for path in (candidate, "/bin/zsh", "/bin/bash", "/bin/sh"):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise TerminalError("no usable shell found")


class TerminalSession:
    def __init__(self, id: str, repo_root: Path, pid: int, fd: int, cols: int, rows: int):
        self.id = id
        self.repo_root = repo_root
        self.pid = pid
        self.fd = fd
        self.cols = cols
        self.rows = rows
        self.started_at = utcnow()
        self._exited = False
        self._closed = False
        self._lock = threading.Lock()

    @classmethod
    def spawn(cls, repo_root: Path, cols: int = 80, rows: int = 24, shell: str | None = None) -> TerminalSession:
        shell = shell or default_shell()
        if not (os.path.isfile(shell) and os.access(shell, os.X_OK)):
            raise TerminalError(f"shell not found: {shell}")
        cols = max(10, min(int(cols), 500))
        rows = max(3, min(int(rows), 200))
        pid, fd = pty.fork()
        if pid == 0:  # child: only exec, never return
            try:
                os.chdir(repo_root)
                env = dict(os.environ)
                env["TERM"] = "xterm-256color"
                env["COLORTERM"] = "truecolor"
                env.pop("PROMPT_COMMAND", None)
                os.execve(shell, [f"-{os.path.basename(shell)}"], env)
            except BaseException:  # noqa: BLE001
                os._exit(127)
        session = cls(id="t_" + secrets.token_hex(6), repo_root=repo_root, pid=pid, fd=fd, cols=cols, rows=rows)
        session.resize(cols, rows)
        log.info("terminal %s spawned pid=%s", session.id, pid)
        return session

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(10, min(int(cols), 500))
        self.rows = max(3, min(int(rows), 200))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        if self._exited:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            self._exited = True

    def read(self, timeout: float = 0.1) -> bytes | None:
        if self._exited:
            return b""
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            self._exited = True
            return b""
        if not ready:
            return None
        try:
            data = os.read(self.fd, READ_CHUNK)
        except OSError:  # EIO once the child is gone
            self._exited = True
            return b""
        if not data:
            self._exited = True
            return b""
        return data

    def alive(self) -> bool:
        if self._exited:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._exited = True
            return False
        if pid != 0:
            self._exited = True
            return False
        return True

    def _signal(self, sig: int) -> bool:
        """Signal the shell's whole process group; False once it is gone."""
        try:
            os.killpg(self.pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            try:
                os.kill(self.pid, sig)
                return True
            except ProcessLookupError:
                return False

    def _reap(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if pid:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def kill(self) -> None:
        """Hang up the terminal and end the shell without ever blocking.

        Closing the pty master is what normally ends the shell. macOS can keep
        a signalled session leader in the exiting state for a long time (it
        was observed with zsh killed during start-up), so the wait is bounded
        and the session is marked dead even if the kernel has not reaped it.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._exited = True
            try:
                os.close(self.fd)
            except OSError:
                pass
            reaped = self._reap(0.3)
            for sig, wait in ((signal.SIGHUP, 0.5), (signal.SIGTERM, 0.5), (signal.SIGKILL, 1.0)):
                if reaped:
                    break
                if not self._signal(sig):
                    reaped = self._reap(0.1)
                    break
                reaped = self._reap(wait)
        if reaped:
            log.info("terminal %s closed", self.id)
        else:
            log.warning("terminal %s shell pid=%s did not exit in time; left for the OS", self.id, self.pid)

    def to_dict(self) -> dict:
        return {"id": self.id, "repo_root": str(self.repo_root), "started_at": self.started_at, "alive": self.alive(), "cols": self.cols, "rows": self.rows}


class TerminalManager:
    def __init__(self, max_sessions: int = 20):
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()
        self._max = max_sessions

    def create(self, repo_root: Path, cols: int, rows: int) -> TerminalSession:
        with self._lock:
            self._reap()
            if len(self._sessions) >= self._max:
                raise TerminalError("too many open terminals; close one first")
            session = TerminalSession.spawn(repo_root, cols, rows)
            self._sessions[session.id] = session
            return session

    def _reap(self) -> None:
        for sid, session in list(self._sessions.items()):
            if not session.alive():
                session.kill()
                self._sessions.pop(sid, None)

    def get(self, session_id: str) -> TerminalSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_for(self, repo_root: Path) -> list[TerminalSession]:
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
