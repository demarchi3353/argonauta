"""Thin wrappers over the git CLI. Every call is argv-based (no shell)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 120


class GitError(RuntimeError):
    pass


def run_git(repo_root: Path, *args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out after {GIT_TIMEOUT}s.") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GitError(f"git {' '.join(args[:2])} failed: {detail or 'unknown error'}")
    return proc


def repo_toplevel(start: Path) -> Path:
    proc = run_git(start, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise GitError(f"{start} is not inside a git repository.")
    return Path(proc.stdout.strip()).resolve()


def current_branch(repo_root: Path) -> str:
    proc = run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if proc.returncode != 0:
        return "(no commits yet)"
    return proc.stdout.strip()


def head_sha(repo_root: Path) -> str | None:
    proc = run_git(repo_root, "rev-parse", "--short", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def has_commits(repo_root: Path) -> bool:
    return head_sha(repo_root) is not None


@dataclass(frozen=True)
class StatusEntry:
    code: str
    path: str


def status_entries(repo_root: Path, paths: list[str] | None = None) -> list[StatusEntry]:
    args = ["status", "--porcelain", "--untracked-files=all"]
    if paths:
        args += ["--", *paths]
    proc = run_git(repo_root, *args)
    entries = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        entries.append(StatusEntry(code=line[:2], path=line[3:]))
    return entries


def ahead_behind(repo_root: Path) -> tuple[int, int] | None:
    proc = run_git(repo_root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}", check=False)
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def ls_files(repo_root: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored files, relative to the repo root."""
    proc = run_git(repo_root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [p for p in proc.stdout.split("\0") if p]


def apply_check(repo_root: Path, diff_text: str, reverse: bool = False) -> str | None:
    """Return None when the diff applies cleanly, else git's explanation."""
    args = ["apply", "--check", "--whitespace=nowarn"]
    if reverse:
        args.append("-R")
    proc = run_git(repo_root, *args, "-", check=False, input_text=diff_text)
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout).strip() or "git apply --check failed"


def apply_diff(repo_root: Path, diff_text: str, reverse: bool = False) -> None:
    args = ["apply", "--whitespace=nowarn"]
    if reverse:
        args.append("-R")
    args.append("-")
    run_git(repo_root, *args, input_text=diff_text)


def stage_paths(repo_root: Path, paths: list[str]) -> None:
    if paths:
        run_git(repo_root, "add", "-A", "--", *paths)


def commit(repo_root: Path, message: str) -> str:
    run_git(repo_root, "commit", "-q", "-m", message)
    proc = run_git(repo_root, "rev-parse", "HEAD")
    return proc.stdout.strip()


def revert_commit(repo_root: Path, sha: str) -> str:
    run_git(repo_root, "revert", "--no-edit", sha)
    proc = run_git(repo_root, "rev-parse", "HEAD")
    return proc.stdout.strip()


def commit_exists(repo_root: Path, sha: str) -> bool:
    proc = run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    return proc.returncode == 0


@dataclass(frozen=True)
class LogEntry:
    sha: str
    short: str
    subject: str
    when: str


def recent_log(repo_root: Path, limit: int = 10) -> list[LogEntry]:
    if not has_commits(repo_root):
        return []
    proc = run_git(repo_root, "log", f"-{limit}", "--format=%H%x1f%h%x1f%s%x1f%ci")
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            out.append(LogEntry(sha=parts[0], short=parts[1], subject=parts[2], when=parts[3]))
    return out


def ensure_excluded(repo_root: Path, pattern: str) -> bool:
    """Add a pattern to .git/info/exclude so the data dir never shows up in status.

    Uses the repo-private exclude file rather than editing the user's .gitignore.
    Returns True if the pattern was added.
    """
    proc = run_git(repo_root, "rev-parse", "--git-path", "info/exclude")
    exclude_path = Path(proc.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = repo_root / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    if pattern in existing.splitlines():
        return False
    with exclude_path.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(f"{pattern}\n")
    return True
