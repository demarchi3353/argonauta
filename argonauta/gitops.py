"""Thin wrappers over the git CLI. Every call is argv-based (no shell)."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 120


class GitError(RuntimeError):
    pass


def run_git(
    repo_root: Path,
    *args: str,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = None
    if env:
        merged_env = {**os.environ, **env}
    if not Path(repo_root).is_dir():
        raise GitError(f"cannot run git in {repo_root}: not a directory")
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            errors="replace",
            input=input_text,
            env=merged_env,
            timeout=timeout or GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc
    except OSError as exc:
        raise GitError(f"cannot run git in {repo_root}: {exc.strerror or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out after {timeout or GIT_TIMEOUT}s.") from exc
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


# --recount: trust the hunk bodies, not the line counts in the headers, which
# small local models frequently get wrong. "lenient" additionally matches
# only one context line on each side (-C1) for diffs whose context is sloppy;
# callers record that choice on the proposal so the user can see it.
_APPLY_BASE = ["--recount", "--whitespace=nowarn"]
LENIENT_ARGS = ["-C1"]


def apply_check(repo_root: Path, diff_text: str, reverse: bool = False, lenient: bool = False) -> str | None:
    """Return None when the diff applies cleanly, else git's explanation."""
    args = ["apply", "--check", *_APPLY_BASE, *(LENIENT_ARGS if lenient else [])]
    if reverse:
        args.append("-R")
    proc = run_git(repo_root, *args, "-", check=False, input_text=diff_text)
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout).strip() or "git apply --check failed"


def apply_mode(repo_root: Path, diff_text: str, reverse: bool = False) -> tuple[str | None, str | None]:
    """Pick the strictest way the diff applies: ("strict"|"lenient"|None, error)."""
    problem = apply_check(repo_root, diff_text, reverse=reverse)
    if problem is None:
        return "strict", None
    if apply_check(repo_root, diff_text, reverse=reverse, lenient=True) is None:
        return "lenient", None
    return None, problem


def apply_diff(repo_root: Path, diff_text: str, reverse: bool = False, lenient: bool = False) -> None:
    args = ["apply", *_APPLY_BASE, *(LENIENT_ARGS if lenient else [])]
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
    run_git(repo_root, "revert", "--no-edit", END, _ref(sha))
    proc = run_git(repo_root, "rev-parse", "HEAD")
    return proc.stdout.strip()


def commit_exists(repo_root: Path, sha: str) -> bool:
    if not sha or sha.startswith("-"):
        return False
    proc = run_git(repo_root, "cat-file", "-e", END, f"{sha}^{{commit}}", check=False)
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


# ----- UI extensions ----------------------------------------------------------
REMOTE_TIMEOUT = 300
REMOTE_ENV = {"GIT_TERMINAL_PROMPT": "0"}
END = "--end-of-options"


def _ref(value: str) -> str:
    """Refuse values that git could read as a flag (defence in depth)."""
    if not value or value.startswith("-"):
        raise GitError(f"invalid ref {value!r}")
    return value


@dataclass(frozen=True)
class BranchInfo:
    name: str
    is_current: bool
    remote: str | None
    upstream: str | None
    sha: str
    subject: str
    ahead: int
    behind: int
    gone: bool


def _parse_track(track: str) -> tuple[int, int, bool]:
    ahead = behind = 0
    gone = "gone" in track
    for part in track.strip("[]").split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part[6:])
        elif part.startswith("behind "):
            behind = int(part[7:])
    return ahead, behind, gone


def list_branches(repo_root: Path) -> list[BranchInfo]:
    fmt = "%(refname)%1f%(refname:short)%1f%(HEAD)%1f%(upstream:short)%1f%(objectname:short)%1f%(subject)%1f%(upstream:track)"
    proc = run_git(repo_root, "for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes")
    out: list[BranchInfo] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 7:
            continue
        refname, short, head, upstream, sha, subject, track = parts
        if refname.endswith("/HEAD"):  # refs/remotes/origin/HEAD shortens to just "origin"
            continue
        remote = None
        if refname.startswith("refs/remotes/"):
            remote = refname[len("refs/remotes/"):].split("/", 1)[0]
        ahead, behind, gone = _parse_track(track)
        out.append(BranchInfo(short, head == "*", remote, upstream or None, sha, subject, ahead, behind, gone))
    return out


@dataclass(frozen=True)
class RemoteInfo:
    name: str
    url: str


def list_remotes(repo_root: Path) -> list[RemoteInfo]:
    proc = run_git(repo_root, "remote", "-v")
    seen: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in seen:
            seen[parts[0]] = parts[1]
    return [RemoteInfo(name, url) for name, url in seen.items()]


@dataclass(frozen=True)
class FileStatus:
    path: str
    index: str
    worktree: str
    untracked: bool
    conflicted: bool
    renamed_from: str | None


_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def status_detail(repo_root: Path) -> list[FileStatus]:
    proc = run_git(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = proc.stdout.split("\0")
    out: list[FileStatus] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        renamed_from = None
        if code[0] in "RC":
            renamed_from = fields[i] if i < len(fields) else None
            i += 1
        out.append(FileStatus(
            path=path,
            index=code[0],
            worktree=code[1],
            untracked=code == "??",
            conflicted=code in _CONFLICT_CODES,
            renamed_from=renamed_from,
        ))
    return out


@dataclass(frozen=True)
class GraphCommit:
    sha: str
    short: str
    parents: list[str]
    refs: list[str]
    author: str
    email: str
    date: str
    subject: str
    signature: str


_LOG_FMT = "%H%x1f%h%x1f%P%x1f%D%x1f%an%x1f%ae%x1f%cI%x1f%s%x1f%G?"


def _parse_log_line(line: str) -> GraphCommit | None:
    parts = line.split("\x1f")
    if len(parts) != 9:
        return None
    sha, short, parents, refs, author, email, date, subject, sig = parts
    return GraphCommit(
        sha=sha,
        short=short,
        parents=parents.split() if parents else [],
        refs=[r.strip() for r in refs.split(",") if r.strip()],
        author=author,
        email=email,
        date=date,
        subject=subject,
        signature=sig or "N",
    )


def graph_log(repo_root: Path, limit: int = 200) -> list[GraphCommit]:
    if not has_commits(repo_root):
        return []
    proc = run_git(repo_root, "log", "--all", "--topo-order", f"-{limit}", f"--format={_LOG_FMT}")
    return [c for c in (_parse_log_line(line) for line in proc.stdout.splitlines()) if c]


@dataclass(frozen=True)
class CommitFile:
    status: str
    path: str
    insertions: int
    deletions: int


@dataclass(frozen=True)
class CommitDetail:
    commit: GraphCommit
    body: str
    files: list[CommitFile]
    diff: str
    signer: str
    key_id: str


def signature_status(repo_root: Path, sha: str) -> tuple[str, str, str]:
    proc = run_git(repo_root, "log", "-1", "--format=%G?%x1f%GS%x1f%GK", END, _ref(sha))
    parts = proc.stdout.strip().split("\x1f")
    while len(parts) < 3:
        parts.append("")
    return parts[0] or "N", parts[1], parts[2]


def commit_detail(repo_root: Path, sha: str) -> CommitDetail:
    proc = run_git(repo_root, "log", "-1", f"--format={_LOG_FMT}", END, _ref(sha))
    commit = _parse_log_line(proc.stdout.strip())
    if commit is None:
        raise GitError(f"commit {sha} not found")
    body = run_git(repo_root, "log", "-1", "--format=%b", END, sha).stdout.strip()
    # git honours only the last of --numstat/--name-status, so ask twice.
    statuses: dict[str, str] = {}
    for line in run_git(repo_root, "show", "--format=", "--name-status", END, sha).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            statuses[parts[-1]] = parts[0][0]
    counts: dict[str, tuple[int, int]] = {}
    for line in run_git(repo_root, "show", "--format=", "--numstat", END, sha).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            ins = int(parts[0]) if parts[0].isdigit() else 0
            dele = int(parts[1]) if parts[1].isdigit() else 0
            counts[parts[2]] = (ins, dele)
    files = [CommitFile(statuses.get(p, "M"), p, c[0], c[1]) for p, c in counts.items()]
    diff = run_git(repo_root, "show", "--format=", "--patch", END, sha).stdout
    _, signer, key_id = signature_status(repo_root, sha)
    return CommitDetail(commit=commit, body=body, files=files, diff=diff, signer=signer, key_id=key_id)


def diff_file(repo_root: Path, path: str, area: str) -> str:
    if area == "index":
        return run_git(repo_root, "diff", "--cached", "--", path).stdout
    tracked = run_git(repo_root, "ls-files", "--error-unmatch", "--", path, check=False).returncode == 0
    if tracked:
        return run_git(repo_root, "diff", "--", path).stdout
    proc = run_git(repo_root, "diff", "--no-index", "--", os.devnull, path, check=False)
    return proc.stdout


def default_base_branch(repo_root: Path) -> str | None:
    """`main` when it exists, else `master`, else None."""
    for name in ("main", "master"):
        if run_git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0:
            return name
    return None


def range_diff(repo_root: Path, base: str) -> str:
    """What HEAD adds since it forked from `base` (`git diff base...HEAD`)."""
    return run_git(repo_root, "diff", END, f"{_ref(base)}...HEAD").stdout


def worktree_diff(repo_root: Path) -> str:
    """Every uncommitted change against HEAD, untracked files included."""
    out = run_git(repo_root, "diff", END, "HEAD").stdout if has_commits(repo_root) else ""
    for entry in status_detail(repo_root):
        if entry.untracked:
            out += diff_file(repo_root, entry.path, "worktree")
    return out


def stage(repo_root: Path, paths: list[str]) -> None:
    if paths:
        run_git(repo_root, "add", "-A", "--", *paths)


def unstage(repo_root: Path, paths: list[str]) -> None:
    if not paths:
        return
    if has_commits(repo_root):
        run_git(repo_root, "reset", "-q", "HEAD", "--", *paths)
    else:
        run_git(repo_root, "rm", "--cached", "-r", "-q", "--", *paths)


def discard(repo_root: Path, paths: list[str]) -> None:
    if not paths:
        return
    status = {e.path: e for e in status_detail(repo_root)}
    untracked = [p for p in paths if p in status and status[p].untracked]
    tracked = [p for p in paths if p not in untracked]
    in_head: list[str] = []
    added_only: list[str] = []
    for p in tracked:
        if has_commits(repo_root) and run_git(repo_root, "cat-file", "-e", f"HEAD:{p}", check=False).returncode == 0:
            in_head.append(p)
        else:
            added_only.append(p)
    if in_head:  # drop both the staged and the working-tree change
        run_git(repo_root, "restore", "--staged", "--worktree", "--source=HEAD", "--", *in_head)
    if added_only:  # a file git has never committed: unstage and delete it
        run_git(repo_root, "rm", "-f", "-q", "--", *added_only)
    if untracked:
        run_git(repo_root, "clean", "-f", "-q", "--", *untracked)


def commit_message(repo_root: Path, message: str, amend: bool = False) -> str:
    args = ["commit", "-q", "-m", message]
    if amend:
        args.append("--amend")
    run_git(repo_root, *args)
    return run_git(repo_root, "rev-parse", "HEAD").stdout.strip()


def valid_branch_name(repo_root: Path, name: str) -> bool:
    if not name or len(name) > 200 or any(ch.isspace() for ch in name):
        return False
    return run_git(repo_root, "check-ref-format", "--branch", name, check=False).returncode == 0


def create_branch(repo_root: Path, name: str, start: str | None = None) -> None:
    args = ["branch", END, _ref(name)]
    if start:
        args.append(_ref(start))
    run_git(repo_root, *args)


def checkout(repo_root: Path, name: str) -> None:
    # `git switch` never interprets its argument as a pathspec, so a file name
    # can never be "checked out" (which would silently discard local edits).
    run_git(repo_root, "switch", "-q", "--no-guess", END, _ref(name))


def delete_branch(repo_root: Path, name: str, force: bool = False) -> None:
    run_git(repo_root, "branch", "-D" if force else "-d", END, _ref(name))


def _remote(repo_root: Path, *args: str) -> str:
    proc = run_git(repo_root, *args, env=REMOTE_ENV, timeout=REMOTE_TIMEOUT)
    return (proc.stdout + proc.stderr).strip()


def fetch(repo_root: Path, remote: str | None = None) -> str:
    return _remote(repo_root, "fetch", "--prune", *([END, _ref(remote)] if remote else []))


def pull(repo_root: Path) -> str:
    return _remote(repo_root, "pull", "--ff-only")


def push(repo_root: Path, set_upstream: bool = False) -> str:
    if set_upstream:
        return _remote(repo_root, "push", "-u", "origin", "HEAD")
    return _remote(repo_root, "push")


# ----- worktrees -------------------------------------------------------------------
WORKTREES_DIR = ".worktrees"
_WORKTREE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str | None
    head: str | None
    is_main: bool
    locked: bool
    prunable: bool


def list_worktrees(repo_root: Path) -> list[WorktreeInfo]:
    proc = run_git(repo_root, "worktree", "list", "--porcelain")
    out: list[WorktreeInfo] = []
    cur: dict = {}
    for line in proc.stdout.splitlines() + [""]:
        if not line:
            if cur:
                out.append(WorktreeInfo(
                    path=Path(cur["worktree"]).resolve(),
                    branch=cur.get("branch", "").replace("refs/heads/", "") or None,
                    head=(cur.get("HEAD") or "")[:7] or None,
                    is_main=len(out) == 0,
                    locked="locked" in cur,
                    prunable="prunable" in cur,
                ))
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    return out


def valid_worktree_name(name: str) -> bool:
    return bool(name) and bool(_WORKTREE_NAME_RE.match(name)) and ".." not in name


def add_worktree(repo_root: Path, name: str, branch: str | None = None, base: str | None = None, new_branch: bool = True) -> Path:
    """Create <repo>/.worktrees/<name>; by default on a new branch named after it."""
    if not valid_worktree_name(name):
        raise GitError(f"invalid worktree name {name!r}: use letters, digits, '.', '_' or '-'")
    branch = branch or name
    if not valid_branch_name(repo_root, branch):
        raise GitError(f"invalid branch name {branch!r}")
    if base is not None and not (valid_branch_name(repo_root, base) or re.match(r"^[0-9a-f]{7,40}$", base)):
        raise GitError(f"invalid base {base!r}")
    path = repo_root / WORKTREES_DIR / name
    if path.exists():
        raise GitError(f"{path} already exists")
    ensure_excluded(repo_root, f"/{WORKTREES_DIR}/")
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add"]
    if new_branch:
        args += ["-b", branch]
    args += [END, str(path)]
    if new_branch:
        if base:
            args.append(base)
    else:
        args.append(branch)
    run_git(repo_root, *args)
    return path.resolve()


def remove_worktree(repo_root: Path, path: Path, force: bool = False) -> None:
    path = Path(path).resolve()
    known = {w.path: w for w in list_worktrees(repo_root)}
    if path not in known or known[path].is_main:
        raise GitError(f"{path} is not a worktree of this repository")
    if not force and status_entries(path):
        raise GitError("the worktree has uncommitted changes; commit them or remove with force")
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    run_git(repo_root, *args, END, str(path))
    run_git(repo_root, "worktree", "prune", check=False)
