"""Windows ConPTY-backed shell sessions for the embedded terminal (via pywinpty).

Mirrors the POSIX session surface so the server and API work unchanged. The
winpty import is deferred to spawn() so this module imports on any platform.
"""

from __future__ import annotations

import os
import secrets
import signal
import threading
import time
from pathlib import Path

from argonauta.logging_setup import get_logger
from argonauta.models import utcnow
from argonauta.web.terminal import MISSING_PYWINPTY, TerminalError, default_shell

log = get_logger()
POWERSHELLS = {"pwsh.exe", "pwsh", "powershell.exe", "powershell"}


def shell_cmdline(shell: str) -> str | None:
    """Arguments for the shell: PowerShell starts without its banner."""
    name = os.path.basename(shell.replace("\\", "/")).lower()
    return "-NoLogo" if name in POWERSHELLS else None


def env_block(extra: dict[str, str]) -> str:
    """The environment as one string of NUL-terminated name=value pairs."""
    return "".join(f"{k}={v}\0" for k, v in {**os.environ, **extra}.items())


class WindowsTerminalSession:
    def __init__(self, id: str, repo_root: Path, pty, cols: int, rows: int):
        self.id = id
        self.repo_root = repo_root
        self.pid = pty.pid
        self.cols = cols
        self.rows = rows
        self.started_at = utcnow()
        self._pty = pty
        self._exited = False
        self._closed = False
        self._lock = threading.Lock()

    @classmethod
    def spawn(cls, repo_root: Path, cols: int = 80, rows: int = 24, shell: str | None = None) -> WindowsTerminalSession:
        try:
            from winpty import PTY, WinptyError
        except ImportError:
            raise TerminalError(MISSING_PYWINPTY) from None
        shell = shell or default_shell()
        if not (os.path.isfile(shell) and os.access(shell, os.X_OK)):
            raise TerminalError(f"shell not found: {shell}")
        cols = max(10, min(int(cols), 500))
        rows = max(3, min(int(rows), 200))
        pty = PTY(cols, rows)
        env = env_block({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        try:
            started = pty.spawn(appname=shell, cmdline=shell_cmdline(shell), cwd=str(repo_root), env=env)
        except WinptyError as exc:
            raise TerminalError(f"could not start {shell}: {exc}") from exc
        if not started:
            raise TerminalError(f"could not start {shell}: spawn returned False")
        session = cls(id="t_" + secrets.token_hex(6), repo_root=repo_root, pty=pty, cols=cols, rows=rows)
        log.info("terminal %s spawned pid=%s", session.id, session.pid)
        return session

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(10, min(int(cols), 500))
        self.rows = max(3, min(int(rows), 200))
        pty = self._pty
        if pty is None:
            return
        try:
            pty.set_size(self.cols, self.rows)
        except Exception:  # noqa: BLE001 - a dying console must not break the caller
            pass

    def write(self, data: bytes) -> None:
        pty = self._pty
        if self._exited or pty is None:
            return
        try:
            pty.write(data.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            self._exited = True

    def read(self, timeout: float = 0.1) -> bytes | None:
        pty = self._pty
        if self._exited or pty is None:
            return b""
        try:
            text = pty.read(blocking=False)
            if text:
                return text.encode("utf-8")
            if pty.iseof() or not pty.isalive():
                self._exited = True
                return b""
        except Exception:  # noqa: BLE001
            self._exited = True
            return b""
        time.sleep(min(timeout, 0.02))
        return None

    def alive(self) -> bool:
        pty = self._pty
        if self._exited or pty is None:
            return False
        try:
            if pty.isalive():
                return True
        except Exception:  # noqa: BLE001
            pass
        self._exited = True
        return False

    def kill(self) -> None:
        """End the shell and drop the console; bounded wait, idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pty = self._pty
            if self.alive():
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except OSError:
                    pass
            self._exited = True
            self._pty = None  # dropping the PTY closes its handles
            gone = pty is None
            deadline = time.monotonic() + 1.0
            while pty is not None and not gone:
                try:
                    gone = not pty.isalive()
                except Exception:  # noqa: BLE001
                    gone = True
                if not gone:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        if gone:
            log.info("terminal %s closed", self.id)
        else:
            log.warning("terminal %s shell pid=%s did not exit in time; left for the OS", self.id, self.pid)

    def to_dict(self) -> dict:
        return {"id": self.id, "repo_root": str(self.repo_root), "started_at": self.started_at, "alive": self.alive(), "cols": self.cols, "rows": self.rows}
