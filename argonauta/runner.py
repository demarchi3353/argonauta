"""Runs the repository's test command without a shell and records the outcome."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from argonauta.config import SECRET_ENV_VARS
from argonauta.models import utcnow

OUTPUT_TAIL_CHARS = 4000


class RunnerError(ValueError):
    pass


@dataclass(frozen=True)
class TestResult:
    command: str
    started_at: str
    finished_at: str
    exit_code: int | None
    status: str  # passed | failed | error
    output_tail: str
    duration_s: float


def parse_command(command: str) -> list[str]:
    command = (command or "").strip()
    if not command:
        raise RunnerError("the test command is empty")
    if len(command) > 1000:
        raise RunnerError("the test command is longer than 1000 characters")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise RunnerError(f"could not parse the test command: {exc}") from exc
    if not argv:
        raise RunnerError("the test command is empty")
    return argv


def check_command(command: str) -> list[str]:
    """Parse the command and make sure its executable can be found.

    Catches the classic "python -m pytest" on a machine that only has python3
    at configuration time instead of at apply time.
    """
    argv = parse_command(command)
    exe = argv[0]
    resolvable = shutil.which(exe) is not None or (os.path.sep in exe and os.path.isfile(os.path.expanduser(exe)) and os.access(os.path.expanduser(exe), os.X_OK))
    if not resolvable:
        hint = ""
        if exe in ("python", "py"):
            rest = " ".join(argv[1:])
            hint = f" Try 'python3 {rest}' or '.venv/bin/python {rest}'.".replace("  ", " ")
        raise RunnerError(f"command not found: {exe}.{hint}")
    return argv


def run_tests(repo_root: Path, command: str, timeout: int = 600) -> TestResult:
    argv = parse_command(command)
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_VARS}
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    started = utcnow()
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return TestResult(
            command=command,
            started_at=started,
            finished_at=utcnow(),
            exit_code=None,
            status="error",
            output_tail=f"command not found: {argv[0]}",
            duration_s=time.monotonic() - t0,
        )
    except subprocess.TimeoutExpired as exc:
        tail = _tail((exc.stdout or "") if isinstance(exc.stdout, str) else "", (exc.stderr or "") if isinstance(exc.stderr, str) else "")
        return TestResult(
            command=command,
            started_at=started,
            finished_at=utcnow(),
            exit_code=None,
            status="error",
            output_tail=f"timed out after {timeout}s\n{tail}",
            duration_s=time.monotonic() - t0,
        )
    status = "passed" if proc.returncode == 0 else "failed"
    return TestResult(
        command=command,
        started_at=started,
        finished_at=utcnow(),
        exit_code=proc.returncode,
        status=status,
        output_tail=_tail(proc.stdout, proc.stderr),
        duration_s=time.monotonic() - t0,
    )


def _tail(stdout: str, stderr: str) -> str:
    combined = stdout
    if stderr.strip():
        combined = f"{stdout}\n--- stderr ---\n{stderr}" if stdout.strip() else stderr
    combined = combined.strip()
    if len(combined) > OUTPUT_TAIL_CHARS:
        combined = "…" + combined[-OUTPUT_TAIL_CHARS:]
    return combined
