# Argonauta Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, single-user web interface to Argonauta with a commit graph, staging and commits, branches and remotes, workspaces, an embedded terminal, a credentials panel and the assistant loop on one page.

**Architecture:** A standard-library `ThreadingHTTPServer` inside the package serves one static page and a JSON API; long git and model operations run as polled background jobs; a minimal RFC 6455 WebSocket bridges pty shells to a vendored xterm.js. The existing `workflow.py`, `db.py` and `gitops.py` stay the single source of truth; the web layer only orchestrates them.

**Tech Stack:** Python 3.12 (`http.server`, `pty`, `select`, `threading`, `hashlib`, `base64`), Typer, SQLite, vanilla HTML/CSS/JS, vendored `@xterm/xterm` 5.5.0 and `@xterm/addon-fit` 0.10.0 (MIT).

**Spec:** `docs/superpowers/specs/2026-10-22-argonauta-web-ui-design.md`

## Global Constraints

- Python `>=3.12`; no new runtime dependency (`typer`, `rich`, `python-dotenv` only; `anthropic` optional).
- Server binds only `127.0.0.1`; `--host` accepts `127.0.0.1` or `localhost` and nothing else.
- Every API and WebSocket request requires `Authorization: Bearer <token>` (WebSocket: first message `{"type":"auth","token":...}`), a `Host` of `127.0.0.1:<port>` or `localhost:<port>`, and for API/WS an `Origin` of `http://127.0.0.1:<port>` or `http://localhost:<port>`.
- JSON bodies capped at 1 MB; commit messages 1..5000 chars; task text goes through `workflow.validate_task`.
- File paths from the client pass `diffs._path_problem` (relative, no `..`, not under `.git/`, `.argonauta/`, `.env*`).
- git always as argv, never a shell; remote operations run with `GIT_TERMINAL_PROMPT=0`.
- No external resources on the page: xterm.js is vendored under `argonauta/web/static/vendor/` with `VERSION` and `LICENSE` files.
- Logs carry ids, paths, counts and status words only. Never tokens, bodies, diffs, file contents or terminal traffic.
- Registry file: `~/.config/argonauta/workspaces.json`, overridable with `ARGONAUTA_CONFIG_DIR`.
- Out of scope: merge, rebase, conflict resolution, stash, hunk staging, tags, submodules, remote branch deletion.

## Review Focus

1. A repository path in the registry that was deleted or moved after registration: the UI must show it as missing and every repo-scoped API call must return 404, not crash. (Task 2 test `test_get_repo_path_missing`, Task 7 test `test_unknown_or_missing_repo_is_404`.)
2. A commit message that is only whitespace or longer than 5000 chars, and a branch name with spaces or `..`: the API must answer 400 with the field name, and git must never be called. (Task 8 tests `test_commit_rejects_bad_message`, `test_branch_name_validated`.)
3. `push` and `pull` against a remote that needs credentials the agent does not have: the job must fail with git's message within the timeout instead of hanging on a prompt. (Task 8 test `test_push_without_credentials_fails_fast` uses an `ssh://` URL to a closed port with `GIT_SSH_COMMAND` forcing BatchMode.)
4. A WebSocket client that sends an unmasked frame, an oversized frame, or closes mid-session: the server must close the socket and kill the shell, never leak a pty. (Task 4 tests `test_unmasked_client_frame_rejected`, `test_oversized_frame_rejected`; Task 9 test `test_terminal_shell_dies_when_socket_closes`.)
5. A request with the right token but a foreign `Origin` (DNS rebinding / another tab) must get 403 and must not run anything. (Task 7 test `test_foreign_origin_rejected`.)

---

### Task 1: gitops extensions

**Files:**
- Modify: `argonauta/gitops.py`
- Test: `tests/test_gitops_ui.py`

**Interfaces:**
- Consumes: existing `run_git(repo_root, *args, check=True, input_text=None)`, `GitError`, `has_commits`.
- Produces:
  - `run_git(..., env: dict | None = None, timeout: int | None = None)`
  - `BranchInfo(name, is_current, remote, upstream, sha, subject, ahead, behind, gone)`; `list_branches(repo_root) -> list[BranchInfo]`
  - `RemoteInfo(name, url)`; `list_remotes(repo_root) -> list[RemoteInfo]`
  - `FileStatus(path, index, worktree, untracked, conflicted, renamed_from)`; `status_detail(repo_root) -> list[FileStatus]`
  - `GraphCommit(sha, short, parents, refs, author, email, date, subject, signature)`; `graph_log(repo_root, limit=200) -> list[GraphCommit]`
  - `CommitDetail(commit, body, files, diff, signer, key_id)`; `CommitFile(status, path, insertions, deletions)`; `commit_detail(repo_root, sha) -> CommitDetail`
  - `diff_file(repo_root, path, area) -> str` with `area in ("worktree", "index")`
  - `stage(repo_root, paths)`, `unstage(repo_root, paths)`, `discard(repo_root, paths)`
  - `commit_message(repo_root, message, amend=False) -> str`
  - `valid_branch_name(repo_root, name) -> bool`, `create_branch(repo_root, name, start=None)`, `checkout(repo_root, name)`, `delete_branch(repo_root, name, force=False)`
  - `fetch(repo_root, remote=None) -> str`, `pull(repo_root) -> str`, `push(repo_root, set_upstream=False) -> str` (return combined output; raise `GitError` on failure)
  - `signature_status(repo_root, sha) -> tuple[str, str, str]` → `(code, signer, key_id)` with code from `%G?`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gitops_ui.py
import os
import subprocess
from pathlib import Path

import pytest

from argonauta import gitops
from tests.conftest import GIT_ENV, git


@pytest.fixture
def repo(sample_repo: Path) -> Path:
    return sample_repo


def test_list_branches_marks_current_and_upstream(repo, tmp_path):
    git(repo, "branch", "feature")
    bare = tmp_path / "remote.git"
    git(repo, "init", "--bare", "-q", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "HEAD")
    branches = gitops.list_branches(repo)
    names = {b.name: b for b in branches}
    current = [b for b in branches if b.is_current]
    assert len(current) == 1 and current[0].upstream == f"origin/{current[0].name}"
    assert names["feature"].remote is None and not names["feature"].is_current
    remote = [b for b in branches if b.remote == "origin"]
    assert remote and remote[0].name.startswith("origin/")
    assert gitops.list_remotes(repo) == [gitops.RemoteInfo(name="origin", url=str(bare))]


def test_status_detail_splits_areas(repo):
    (repo / "calc" / "core.py").write_text("changed\n")
    (repo / "new.txt").write_text("hi\n")
    (repo / "README.md").write_text("staged\n")
    git(repo, "add", "README.md")
    entries = {e.path: e for e in gitops.status_detail(repo)}
    assert entries["calc/core.py"].worktree == "M" and entries["calc/core.py"].index == " "
    assert entries["README.md"].index == "M" and entries["README.md"].worktree == " "
    assert entries["new.txt"].untracked and not entries["new.txt"].conflicted


def test_graph_log_has_parents_refs_and_signature_code(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("f\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "feature commit")
    commits = gitops.graph_log(repo)
    assert commits[0].subject == "feature commit"
    assert commits[0].parents == [commits[1].sha]
    assert any("feature" in r for r in commits[0].refs)
    assert commits[0].signature in ("N", "G", "B", "U", "X", "Y", "R", "E")
    assert commits[-1].parents == []


def test_commit_detail_lists_files_and_diff(repo):
    (repo / "calc" / "core.py").write_text("changed\n")
    git(repo, "commit", "-q", "-am", "edit core")
    sha = git(repo, "rev-parse", "HEAD").strip()
    detail = gitops.commit_detail(repo, sha)
    assert detail.commit.sha == sha and detail.commit.subject == "edit core"
    assert [f.path for f in detail.files] == ["calc/core.py"]
    assert detail.files[0].deletions > 0 and "diff --git a/calc/core.py" in detail.diff


def test_diff_file_for_untracked_and_index(repo):
    (repo / "new.txt").write_text("hello\n")
    assert "+hello" in gitops.diff_file(repo, "new.txt", "worktree")
    (repo / "README.md").write_text("staged\n")
    git(repo, "add", "README.md")
    assert "+staged" in gitops.diff_file(repo, "README.md", "index")
    assert gitops.diff_file(repo, "README.md", "worktree") == ""


def test_stage_unstage_discard_and_commit(repo):
    (repo / "README.md").write_text("x\n")
    (repo / "junk.txt").write_text("junk\n")
    gitops.stage(repo, ["README.md"])
    assert {e.path: e.index for e in gitops.status_detail(repo)}["README.md"] == "M"
    gitops.unstage(repo, ["README.md"])
    assert {e.path: e.worktree for e in gitops.status_detail(repo)}["README.md"] == "M"
    gitops.discard(repo, ["README.md", "junk.txt"])
    assert gitops.status_detail(repo) == []
    (repo / "README.md").write_text("y\n")
    gitops.stage(repo, ["README.md"])
    sha = gitops.commit_message(repo, "manual commit")
    assert git(repo, "log", "-1", "--format=%s").strip() == "manual commit" and len(sha) == 40


def test_branch_lifecycle_and_name_validation(repo):
    assert gitops.valid_branch_name(repo, "feat/ok")
    assert not gitops.valid_branch_name(repo, "bad name")
    assert not gitops.valid_branch_name(repo, "a..b")
    gitops.create_branch(repo, "feat/ok")
    gitops.checkout(repo, "feat/ok")
    assert gitops.current_branch(repo) == "feat/ok"
    with pytest.raises(gitops.GitError):
        gitops.delete_branch(repo, "feat/ok")  # current branch
    gitops.checkout(repo, "main" if "main" in git(repo, "branch").split() else "master")
    gitops.delete_branch(repo, "feat/ok")
    assert "feat/ok" not in [b.name for b in gitops.list_branches(repo)]


def test_fetch_pull_push_with_local_bare_remote(repo, tmp_path):
    bare = tmp_path / "remote.git"
    git(repo, "init", "--bare", "-q", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    out = gitops.push(repo, set_upstream=True)
    assert "origin" in out or out == ""
    assert gitops.fetch(repo) is not None
    assert gitops.pull(repo) is not None
    assert gitops.ahead_behind(repo) == (0, 0)


def test_remote_ops_do_not_prompt(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "ssh://git@127.0.0.1:9/none.git")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=2")
    with pytest.raises(gitops.GitError):
        gitops.fetch(repo)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_gitops_ui.py -q`
Expected: FAIL with `AttributeError: module 'argonauta.gitops' has no attribute 'list_branches'` (and similar).

- [ ] **Step 3: Extend `run_git` and add the new functions**

Replace the `run_git` definition in `argonauta/gitops.py` with:

```python
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
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out after {timeout or GIT_TIMEOUT}s.") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GitError(f"git {' '.join(args[:2])} failed: {detail or 'unknown error'}")
    return proc
```

Add `import os` at the top. Append to the end of the file:

```python
# ----- UI extensions ----------------------------------------------------------
REMOTE_TIMEOUT = 300
REMOTE_ENV = {"GIT_TERMINAL_PROMPT": "0"}


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
        if short.endswith("/HEAD"):
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
    proc = run_git(repo_root, "log", "-1", "--format=%G?%x1f%GS%x1f%GK", sha)
    parts = proc.stdout.strip().split("\x1f")
    while len(parts) < 3:
        parts.append("")
    return parts[0] or "N", parts[1], parts[2]


def commit_detail(repo_root: Path, sha: str) -> CommitDetail:
    proc = run_git(repo_root, "log", "-1", f"--format={_LOG_FMT}", sha)
    commit = _parse_log_line(proc.stdout.strip())
    if commit is None:
        raise GitError(f"commit {sha} not found")
    body = run_git(repo_root, "log", "-1", "--format=%b", sha).stdout.strip()
    stat = run_git(repo_root, "show", "--format=", "--numstat", "--name-status", sha).stdout
    statuses: dict[str, str] = {}
    counts: dict[str, tuple[int, int]] = {}
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] and parts[0][0] in "AMDRTC" and not parts[0].isdigit():
            statuses[parts[-1]] = parts[0][0]
        elif len(parts) == 3:
            ins = int(parts[0]) if parts[0].isdigit() else 0
            dele = int(parts[1]) if parts[1].isdigit() else 0
            counts[parts[2]] = (ins, dele)
    files = [CommitFile(statuses.get(p, "M"), p, c[0], c[1]) for p, c in counts.items()]
    diff = run_git(repo_root, "show", "--format=", "--patch", sha).stdout
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
    if tracked:
        run_git(repo_root, "checkout", "--", *tracked)
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
    args = ["branch", name]
    if start:
        args.append(start)
    run_git(repo_root, *args)


def checkout(repo_root: Path, name: str) -> None:
    run_git(repo_root, "checkout", "-q", name)


def delete_branch(repo_root: Path, name: str, force: bool = False) -> None:
    run_git(repo_root, "branch", "-D" if force else "-d", name)


def _remote(repo_root: Path, *args: str) -> str:
    proc = run_git(repo_root, *args, env=REMOTE_ENV, timeout=REMOTE_TIMEOUT)
    return (proc.stdout + proc.stderr).strip()


def fetch(repo_root: Path, remote: str | None = None) -> str:
    return _remote(repo_root, "fetch", "--prune", *([remote] if remote else []))


def pull(repo_root: Path) -> str:
    return _remote(repo_root, "pull", "--ff-only")


def push(repo_root: Path, set_upstream: bool = False) -> str:
    if set_upstream:
        return _remote(repo_root, "push", "-u", "origin", "HEAD")
    return _remote(repo_root, "push")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_gitops_ui.py tests/test_workflow.py -q`
Expected: all PASS. If `test_fetch_pull_push_with_local_bare_remote` fails on `pull` because the bare remote has no branch for `--ff-only`, the push with `-u` must have happened first; check that `push(set_upstream=True)` ran before `pull`.

- [ ] **Step 5: Commit**

```bash
git add argonauta/gitops.py tests/test_gitops_ui.py
git commit -m "Extend gitops with branches, graph, status areas, staging and remotes"
```

---

### Task 2: Workspace registry and CLI commands

**Files:**
- Create: `argonauta/workspaces.py`
- Modify: `argonauta/cli.py` (add `workspace` sub-app)
- Test: `tests/test_workspaces.py`

**Interfaces:**
- Consumes: `gitops.repo_toplevel(path) -> Path`, `gitops.GitError`.
- Produces:
  - `config_dir() -> Path` (env `ARGONAUTA_CONFIG_DIR` or `~/.config/argonauta`)
  - `RegistryError(ValueError)`
  - `Repo(id, path, name)`, `Workspace(id, name, repos)`
  - `Registry(path)`: `.workspaces: list[Workspace]`, `.load()`, `.save()`, `.add_workspace(name) -> Workspace`, `.rename_workspace(id, name)`, `.remove_workspace(id)`, `.add_repo(workspace_id, path) -> Repo`, `.remove_repo(repo_id)`, `.get_repo(repo_id) -> Repo | None`, `.get_workspace(id) -> Workspace | None`, `.find_repo_by_path(path) -> Repo | None`, `.ensure_repo(path) -> Repo` (adds to "Default" if unknown), `.to_dict() -> dict`
  - `open_registry() -> Registry` (uses `config_dir() / "workspaces.json"`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspaces.py
import json
from pathlib import Path

import pytest

from argonauta.workspaces import Registry, RegistryError, config_dir, open_registry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path / "cfg"))
    return open_registry()


def test_config_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path / "cfg"))
    assert config_dir() == tmp_path / "cfg"


def test_registry_starts_empty_and_persists(registry, sample_repo):
    assert registry.workspaces == []
    ws = registry.add_workspace("Personal")
    repo = registry.add_repo(ws.id, sample_repo)
    assert repo.name == sample_repo.name and repo.id.startswith("r_") and ws.id.startswith("w_")
    reloaded = Registry(registry.path)
    reloaded.load()
    assert reloaded.get_repo(repo.id).path == sample_repo.resolve()
    data = json.loads(registry.path.read_text())
    assert data["version"] == 1 and data["workspaces"][0]["name"] == "Personal"


def test_add_repo_validates_git_toplevel(registry, tmp_path, sample_repo):
    ws = registry.add_workspace("W")
    with pytest.raises(RegistryError, match="git repository"):
        registry.add_repo(ws.id, tmp_path)
    sub = sample_repo / "calc"
    repo = registry.add_repo(ws.id, sub)
    assert repo.path == sample_repo.resolve()  # normalised to the toplevel
    with pytest.raises(RegistryError, match="already"):
        registry.add_repo(ws.id, sample_repo)
    with pytest.raises(RegistryError, match="workspace"):
        registry.add_repo("w_nope", sample_repo)


def test_remove_and_rename(registry, sample_repo):
    ws = registry.add_workspace("W")
    repo = registry.add_repo(ws.id, sample_repo)
    registry.rename_workspace(ws.id, "Renamed")
    assert registry.get_workspace(ws.id).name == "Renamed"
    registry.remove_repo(repo.id)
    assert registry.get_repo(repo.id) is None
    registry.remove_workspace(ws.id)
    assert registry.workspaces == []
    with pytest.raises(RegistryError):
        registry.remove_workspace(ws.id)


def test_workspace_name_validation(registry):
    with pytest.raises(RegistryError):
        registry.add_workspace("   ")
    with pytest.raises(RegistryError):
        registry.add_workspace("x" * 101)


def test_ensure_repo_adds_to_default(registry, sample_repo):
    repo = registry.ensure_repo(sample_repo)
    assert registry.workspaces[0].name == "Default"
    assert registry.ensure_repo(sample_repo).id == repo.id


def test_get_repo_path_missing(registry, sample_repo, tmp_path):
    import shutil
    ws = registry.add_workspace("W")
    repo = registry.add_repo(ws.id, sample_repo)
    shutil.rmtree(sample_repo)
    d = registry.to_dict()
    entry = d["workspaces"][0]["repos"][0]
    assert entry["id"] == repo.id and entry["exists"] is False and entry["initialized"] is False


def test_corrupt_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "workspaces.json").write_text("{not json")
    with pytest.raises(RegistryError, match="not valid"):
        open_registry()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workspaces.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'argonauta.workspaces'`.

- [ ] **Step 3: Implement the registry**

```python
# argonauta/workspaces.py
"""User-level registry of workspaces (named groups of repositories).

Stored outside any repository at ~/.config/argonauta/workspaces.json so the
web UI can switch between repositories. Only paths that resolve to a git
toplevel are accepted, and the API refers to repositories by id only.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from argonauta import gitops
from argonauta.config import DATA_DIR_NAME, DB_FILE_NAME

FILE_NAME = "workspaces.json"
VERSION = 1
MAX_NAME = 100
DEFAULT_WORKSPACE = "Default"


class RegistryError(ValueError):
    pass


def config_dir() -> Path:
    raw = os.environ.get("ARGONAUTA_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".config" / "argonauta"


@dataclass
class Repo:
    id: str
    path: Path
    name: str

    @property
    def exists(self) -> bool:
        return (self.path / ".git").exists()

    @property
    def initialized(self) -> bool:
        return (self.path / DATA_DIR_NAME / DB_FILE_NAME).is_file()

    def to_dict(self) -> dict:
        return {"id": self.id, "path": str(self.path), "name": self.name, "exists": self.exists, "initialized": self.initialized}


@dataclass
class Workspace:
    id: str
    name: str
    repos: list[Repo] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "repos": [r.to_dict() for r in self.repos]}


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise RegistryError("name must not be empty")
    if len(name) > MAX_NAME:
        raise RegistryError(f"name must be at most {MAX_NAME} characters")
    return name


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.workspaces: list[Workspace] = []

    # ----- persistence -----
    def load(self) -> None:
        if not self.path.is_file():
            self.workspaces = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("workspaces"), list):
            raise RegistryError(f"{self.path} is not a valid workspaces file")
        out: list[Workspace] = []
        for w in data["workspaces"]:
            if not isinstance(w, dict) or not isinstance(w.get("id"), str) or not isinstance(w.get("name"), str):
                raise RegistryError(f"{self.path} contains an invalid workspace")
            repos = []
            for r in w.get("repos", []):
                if not isinstance(r, dict) or not all(isinstance(r.get(k), str) for k in ("id", "path", "name")):
                    raise RegistryError(f"{self.path} contains an invalid repository entry")
                repos.append(Repo(id=r["id"], path=Path(r["path"]), name=r["name"]))
            out.append(Workspace(id=w["id"], name=w["name"], repos=repos))
        self.workspaces = out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "workspaces": [
            {"id": w.id, "name": w.name, "repos": [{"id": r.id, "path": str(r.path), "name": r.name} for r in w.repos]}
            for w in self.workspaces
        ]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def to_dict(self) -> dict:
        return {"version": VERSION, "workspaces": [w.to_dict() for w in self.workspaces]}

    # ----- lookups -----
    def get_workspace(self, workspace_id: str) -> Workspace | None:
        return next((w for w in self.workspaces if w.id == workspace_id), None)

    def get_repo(self, repo_id: str) -> Repo | None:
        for w in self.workspaces:
            for r in w.repos:
                if r.id == repo_id:
                    return r
        return None

    def find_repo_by_path(self, path: Path) -> Repo | None:
        resolved = path.resolve()
        for w in self.workspaces:
            for r in w.repos:
                if r.path == resolved:
                    return r
        return None

    # ----- mutations -----
    def add_workspace(self, name: str) -> Workspace:
        ws = Workspace(id="w_" + secrets.token_hex(4), name=_clean_name(name))
        self.workspaces.append(ws)
        self.save()
        return ws

    def rename_workspace(self, workspace_id: str, name: str) -> Workspace:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise RegistryError(f"no workspace {workspace_id}")
        ws.name = _clean_name(name)
        self.save()
        return ws

    def remove_workspace(self, workspace_id: str) -> None:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise RegistryError(f"no workspace {workspace_id}")
        self.workspaces.remove(ws)
        self.save()

    def add_repo(self, workspace_id: str, path: Path) -> Repo:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise RegistryError(f"no workspace {workspace_id}")
        try:
            top = gitops.repo_toplevel(Path(path).expanduser())
        except gitops.GitError as exc:
            raise RegistryError(f"{path} is not inside a git repository") from exc
        if self.find_repo_by_path(top) is not None:
            raise RegistryError(f"{top} is already registered")
        repo = Repo(id="r_" + secrets.token_hex(4), path=top, name=top.name)
        ws.repos.append(repo)
        self.save()
        return repo

    def remove_repo(self, repo_id: str) -> None:
        for w in self.workspaces:
            for r in w.repos:
                if r.id == repo_id:
                    w.repos.remove(r)
                    self.save()
                    return
        raise RegistryError(f"no repository {repo_id}")

    def ensure_repo(self, path: Path) -> Repo:
        top = gitops.repo_toplevel(Path(path))
        existing = self.find_repo_by_path(top)
        if existing is not None:
            return existing
        default = next((w for w in self.workspaces if w.name == DEFAULT_WORKSPACE), None)
        if default is None:
            default = self.add_workspace(DEFAULT_WORKSPACE)
        return self.add_repo(default.id, top)


def open_registry() -> Registry:
    registry = Registry(config_dir() / FILE_NAME)
    registry.load()
    return registry
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workspaces.py -q`
Expected: 8 PASS.

- [ ] **Step 5: Add the `workspace` CLI sub-app**

In `argonauta/cli.py`, after `config_app` is created and added, add:

```python
workspace_app = typer.Typer(help="Manage workspaces (groups of repositories) for the web UI.", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")


@workspace_app.command("list")
def workspace_list() -> None:
    """List workspaces and their repositories."""
    from argonauta.workspaces import RegistryError, open_registry

    try:
        registry = open_registry()
    except RegistryError as exc:
        _fail(exc, EXIT_INVALID)
    if not registry.workspaces:
        ui.empty_state("no workspaces", "Add one with `argonauta workspace add PATH` or from the web UI (`argonauta ui`).")
        return
    t = ui.table("Workspaces", ["workspace", "repository", "path", "store"])
    for ws in registry.workspaces:
        if not ws.repos:
            t.add_row(ws.name, "(empty)", "", "")
        for r in ws.repos:
            t.add_row(ws.name, r.name, str(r.path), ("initialised" if r.initialized else "not initialised") if r.exists else "missing")
    ui.print_table(t)
    ui.info(f"Registry: {registry.path}")


@workspace_app.command("add")
def workspace_add(
    path: Annotated[Path, typer.Argument(help="Path inside a git repository.")],
    workspace: Annotated[str, typer.Option("--workspace", "-w", help="Workspace name (created if missing).")] = "Default",
) -> None:
    """Register a repository in a workspace."""
    from argonauta.workspaces import RegistryError, open_registry

    try:
        registry = open_registry()
        ws = next((w for w in registry.workspaces if w.name == workspace), None) or registry.add_workspace(workspace)
        repo = registry.add_repo(ws.id, path)
    except RegistryError as exc:
        _fail(exc, EXIT_INVALID)
    ui.success(f"Added {repo.path} to workspace {ws.name!r} as {repo.id}")


@workspace_app.command("remove")
def workspace_remove(path: Annotated[Path, typer.Argument(help="Path of a registered repository.")]) -> None:
    """Remove a repository from every workspace."""
    from argonauta.workspaces import RegistryError, open_registry

    try:
        registry = open_registry()
        repo = registry.find_repo_by_path(path)
        if repo is None:
            raise RegistryError(f"{path} is not registered")
        registry.remove_repo(repo.id)
    except RegistryError as exc:
        _fail(exc, EXIT_INVALID)
    ui.success(f"Removed {repo.path}")
```

- [ ] **Step 6: Verify the CLI manually and run the suite**

Run: `ARGONAUTA_CONFIG_DIR=/tmp/argo-cfg .venv/bin/python -m argonauta workspace add . && ARGONAUTA_CONFIG_DIR=/tmp/argo-cfg .venv/bin/python -m argonauta workspace list && .venv/bin/python -m pytest -q`
Expected: success line, a table with one row, all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add argonauta/workspaces.py argonauta/cli.py tests/test_workspaces.py
git commit -m "Add workspace registry and workspace CLI commands"
```

---

### Task 3: Background job runner

**Files:**
- Create: `argonauta/web/__init__.py` (empty docstring module)
- Create: `argonauta/web/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `JobConflict(RuntimeError)` with attribute `.job: Job`
  - `Job` dataclass: `id: str, kind: str, repo_id: str | None, status: str ("queued"|"running"|"done"|"failed"), progress: str, result: dict | None, error: str | None, created_at: str, finished_at: str | None`, `.to_dict() -> dict`
  - `JobRunner(max_jobs=200)`: `.submit(kind, repo_id, fn) -> Job` where `fn(progress: Callable[[str], None]) -> dict`; `.get(job_id) -> Job | None`; `.running_for(repo_id) -> Job | None`; `.wait(job_id, timeout=30) -> Job` (tests only); `.shutdown()`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_jobs.py
import threading
import time

import pytest

from argonauta.web.jobs import JobConflict, JobRunner


def test_job_runs_and_reports_result():
    runner = JobRunner()
    def work(progress):
        progress("half")
        return {"ok": True}
    job = runner.submit("test", "r_1", work)
    done = runner.wait(job.id)
    assert done.status == "done" and done.result == {"ok": True} and done.progress == "half"
    assert done.to_dict()["id"] == job.id and done.finished_at


def test_job_failure_is_captured_as_message_not_traceback():
    runner = JobRunner()
    def boom(progress):
        raise ValueError("bad input")
    job = runner.wait(runner.submit("test", None, boom).id)
    assert job.status == "failed" and job.error == "bad input" and job.result is None


def test_one_running_job_per_repo():
    runner = JobRunner()
    gate = threading.Event()
    def slow(progress):
        gate.wait(5)
        return {}
    first = runner.submit("a", "r_1", slow)
    with pytest.raises(JobConflict) as exc:
        runner.submit("b", "r_1", slow)
    assert exc.value.job.id == first.id
    other = runner.submit("c", "r_2", slow)  # other repo is fine
    gate.set()
    assert runner.wait(first.id).status == "done" and runner.wait(other.id).status == "done"
    assert runner.running_for("r_1") is None


def test_old_jobs_are_dropped():
    runner = JobRunner(max_jobs=3)
    ids = [runner.wait(runner.submit("t", None, lambda p: {}).id).id for _ in range(5)]
    assert runner.get(ids[0]) is None and runner.get(ids[-1]) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_jobs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'argonauta.web'`.

- [ ] **Step 3: Implement the runner**

```python
# argonauta/web/__init__.py
"""Local web interface: HTTP server, JSON API, jobs, WebSocket terminals."""
```

```python
# argonauta/web/jobs.py
"""Background jobs for long operations (index, propose, tests, fetch, pull, push).

One thread per job, one running job per repository, results polled over HTTP.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from argonauta.logging_setup import get_logger
from argonauta.models import utcnow

log = get_logger()
JobFn = Callable[[Callable[[str], None]], dict]


@dataclass
class Job:
    id: str
    kind: str
    repo_id: str | None
    status: str = "queued"
    progress: str = ""
    result: dict | None = None
    error: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "repo_id": self.repo_id, "status": self.status,
            "progress": self.progress, "result": self.result, "error": self.error,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }


class JobConflict(RuntimeError):
    def __init__(self, job: Job):
        super().__init__(f"a {job.kind} job is already running for this repository")
        self.job = job


class JobRunner:
    def __init__(self, max_jobs: int = 200):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._max = max_jobs

    def running_for(self, repo_id: str | None) -> Job | None:
        if repo_id is None:
            return None
        with self._lock:
            for job in self._jobs.values():
                if job.repo_id == repo_id and job.status in ("queued", "running"):
                    return job
        return None

    def submit(self, kind: str, repo_id: str | None, fn: JobFn) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if repo_id is not None and job.repo_id == repo_id and job.status in ("queued", "running"):
                    raise JobConflict(job)
            job = Job(id="j_" + secrets.token_hex(6), kind=kind, repo_id=repo_id, created_at=utcnow())
            self._jobs[job.id] = job
            while len(self._jobs) > self._max:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if oldest.status in ("queued", "running"):
                    break
                self._jobs.popitem(last=False)
                self._threads.pop(oldest_id, None)
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{job.kind}", daemon=True)
        self._threads[job.id] = thread
        thread.start()
        return job

    def _run(self, job: Job, fn: JobFn) -> None:
        job.status = "running"

        def progress(text: str) -> None:
            job.progress = str(text)[:200]

        try:
            result = fn(progress)
            job.result = result if isinstance(result, dict) else {}
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - every failure becomes a message for the UI
            job.error = str(exc) or exc.__class__.__name__
            job.status = "failed"
            log.warning("job %s %s failed", job.id, job.kind)
        finally:
            job.finished_at = utcnow()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout: float = 30) -> Job:
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def shutdown(self) -> None:
        for thread in list(self._threads.values()):
            thread.join(0.1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_jobs.py -q`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add argonauta/web/__init__.py argonauta/web/jobs.py tests/test_jobs.py
git commit -m "Add background job runner for the web UI"
```

---

### Task 4: Minimal WebSocket (RFC 6455)

**Files:**
- Create: `argonauta/web/websocket.py`
- Test: `tests/test_websocket.py`

**Interfaces:**
- Produces:
  - `ProtocolError(Exception)`, `ConnectionClosed(Exception)`
  - `OP_CONT=0, OP_TEXT=1, OP_BINARY=2, OP_CLOSE=8, OP_PING=9, OP_PONG=10`
  - `accept_key(client_key: str) -> str`
  - `Frame(opcode: int, payload: bytes, fin: bool)`
  - `encode_frame(opcode, payload: bytes, mask: bool = False) -> bytes`
  - `read_frame(stream, require_mask: bool = True) -> Frame` (`stream.read(n)` like `rfile`)
  - `WebSocketConnection(rfile, wfile)`: `.recv() -> Frame` (only TEXT/BINARY; handles PING/PONG/CONT; raises `ConnectionClosed` on CLOSE or EOF), `.send_text(str)`, `.send_binary(bytes)`, `.close(code=1000)`; thread-safe sends.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_websocket.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_websocket.py -q`
Expected: FAIL with `ImportError: cannot import name 'websocket'`.

- [ ] **Step 3: Implement the protocol**

```python
# argonauta/web/websocket.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_websocket.py -q`
Expected: 12 PASS (parametrised).

- [ ] **Step 5: Commit**

```bash
git add argonauta/web/websocket.py tests/test_websocket.py
git commit -m "Add minimal RFC 6455 WebSocket implementation"
```

---

### Task 5: Pty terminal sessions

**Files:**
- Create: `argonauta/web/terminal.py`
- Test: `tests/test_terminal.py`

**Interfaces:**
- Produces:
  - `TerminalError(RuntimeError)`
  - `TerminalSession`: attributes `id: str, repo_root: Path, pid: int, fd: int, started_at: str, cols: int, rows: int`; `TerminalSession.spawn(repo_root, cols=80, rows=24, shell=None) -> TerminalSession`; `.resize(cols, rows)`, `.write(data: bytes)`, `.read(timeout: float = 0.1) -> bytes | None` (`None` = nothing yet, `b""` = closed), `.alive() -> bool`, `.kill()`
  - `TerminalManager(max_sessions=20)`: `.create(repo_root, cols, rows) -> TerminalSession`, `.get(id) -> TerminalSession | None`, `.remove(id)`, `.list_for(repo_root) -> list[TerminalSession]`, `.shutdown()`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_terminal.py
import os
import time

import pytest

from argonauta.web.terminal import TerminalError, TerminalManager, TerminalSession


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


def test_exit_is_detected(tmp_path):
    session = TerminalSession.spawn(tmp_path, shell="/bin/sh")
    session.write(b"exit\n")
    deadline = time.monotonic() + 5
    while session.alive() and time.monotonic() < deadline:
        session.read(0.1)
    assert not session.alive()
    session.kill()  # idempotent


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


def test_bad_shell_raises(tmp_path):
    with pytest.raises(TerminalError):
        TerminalSession.spawn(tmp_path, shell="/nonexistent/shell")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_terminal.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement sessions**

```python
# argonauta/web/terminal.py
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

    def kill(self) -> None:
        with self._lock:
            if self.alive():
                try:
                    os.kill(self.pid, signal.SIGHUP)
                    for _ in range(20):
                        if not self.alive():
                            break
                        time.sleep(0.05)
                    if self.alive():
                        os.kill(self.pid, signal.SIGKILL)
                        os.waitpid(self.pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
            self._exited = True
            try:
                os.close(self.fd)
            except OSError:
                pass
        log.info("terminal %s closed", self.id)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_terminal.py -q`
Expected: 4 PASS. If `test_spawn_runs_a_shell_in_the_repo` cannot find the tmp path in the output because macOS resolves `/var` to `/private/var`, the assertion already uses `tmp_path.resolve()`; keep it.

- [ ] **Step 5: Commit**

```bash
git add argonauta/web/terminal.py tests/test_terminal.py
git commit -m "Add pty terminal sessions for the embedded terminal"
```

---

### Task 6: Credentials inspection

**Files:**
- Create: `argonauta/web/credentials.py`
- Test: `tests/test_credentials.py`

**Interfaces:**
- Consumes: `gitops.list_remotes`, `gitops.signature_status`, `gitops.head_sha`.
- Produces:
  - `Runner = Callable[[list[str], Path | None], tuple[int, str]]`; `run_cmd(argv, cwd=None, timeout=10) -> (returncode, stdout+stderr)`; missing binary → `(127, "")`
  - pure parsers: `remote_kind(url) -> tuple[str, str | None]` (`("ssh", host_alias)`, `("https", host)`, `("local", None)`, `("none", None)`); `parse_ssh_config(text) -> dict` with `hostname`, `user`, `identity_files: list[str]`; `parse_ssh_add(text) -> list[str]` (fingerprints); `parse_keygen(text) -> dict` with `bits`, `fingerprint`, `comment`, `type`; `parse_gpg_colons(text) -> dict | None` with `key_id`, `fingerprint`, `uid`, `created`, `expires`, `status`; `parse_config_origin(text) -> tuple[str | None, str | None]` → `(value, source)`
  - `SIGNATURE_WORDS: dict[str, str]` mapping `%G?` codes to `good|bad|good_untrusted|expired_signature|expired_key|revoked|unknown_key|unsigned`
  - `inspect(repo_root, run: Runner = run_cmd) -> dict` with keys `identity`, `ssh`, `signing`, `head` exactly as in the spec.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_credentials.py
from pathlib import Path

from argonauta.web import credentials as c

SSH_G = """user git
hostname github.com
port 22
identityfile ~/.ssh/personal
identityfile ~/.ssh/id_ed25519
identitiesonly yes
"""
SSH_ADD = "256 SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE demarchi@example (ED25519)\n"
KEYGEN = "256 SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE demarchi@example (ED25519)\n"
GPG = """tru::1:1718000000:0:3:1:5
pub:u:4096:1:D519886F454D7B57:1718000000:1800000000::u:::scESC::::::23::0:
fpr:::::::::DACD5A577C1C74DEFB025B7FD519886F454D7B57:
uid:u::::1718000000::ABCDEF::gpg-personal <me@example.invalid>::::::::::0:
sub:u:4096:1:AAAAAAAAAAAAAAAA:1718000000::::::e::::::23:
"""


def test_remote_kind():
    assert c.remote_kind("git@github-personal:me/repo.git") == ("ssh", "github-personal")
    assert c.remote_kind("ssh://git@example.com:2222/x.git") == ("ssh", "example.com")
    assert c.remote_kind("https://github.com/me/repo.git") == ("https", "github.com")
    assert c.remote_kind("/tmp/remote.git") == ("local", None)
    assert c.remote_kind("") == ("none", None)


def test_parsers():
    cfg = c.parse_ssh_config(SSH_G)
    assert cfg["hostname"] == "github.com" and cfg["user"] == "git"
    assert cfg["identity_files"] == ["~/.ssh/personal", "~/.ssh/id_ed25519"]
    assert c.parse_ssh_add(SSH_ADD) == ["SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE"]
    assert c.parse_ssh_add("The agent has no identities.\n") == []
    key = c.parse_keygen(KEYGEN)
    assert key == {"bits": 256, "fingerprint": "SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE", "comment": "demarchi@example", "type": "ED25519"}
    gpg = c.parse_gpg_colons(GPG)
    assert gpg["key_id"] == "D519886F454D7B57" and gpg["fingerprint"].endswith("454D7B57")
    assert gpg["uid"] == "gpg-personal <me@example.invalid>" and gpg["status"] == "ok"
    assert gpg["created"].startswith("2024") and gpg["expires"].startswith("2027")
    assert c.parse_gpg_colons("") is None
    assert c.parse_config_origin("file:/Users/me/.gitconfig\tJane\n") == ("Jane", "~/.gitconfig")
    assert c.parse_config_origin("file:.git/config\tJane\n") == ("Jane", "repo .git/config")
    assert c.parse_config_origin("") == (None, None)


def test_inspect_composes_with_fake_runner(sample_repo, tmp_path, monkeypatch):
    from tests.conftest import git
    git(sample_repo, "remote", "add", "origin", "git@github-personal:me/repo.git")
    git(sample_repo, "config", "user.name", "test")  # the fixture commits via env vars, not config
    git(sample_repo, "config", "user.signingkey", "D519886F454D7B57")
    git(sample_repo, "config", "commit.gpgsign", "true")
    pub = tmp_path / "personal.pub"
    pub.write_text("ssh-ed25519 AAAA demarchi@example\n")
    monkeypatch.setattr(c, "_expand", lambda p: str(pub)[:-4] if p.endswith("personal") else p)

    def fake_run(argv, cwd=None):
        if argv[:2] == ["ssh", "-G"]:
            return 0, SSH_G
        if argv == ["ssh-add", "-l"]:
            return 0, SSH_ADD
        if argv[:2] == ["ssh-keygen", "-l"]:
            return 0, KEYGEN
        if argv[0] == "gpg":
            return 0, GPG
        return c.run_cmd(argv, cwd)

    info = c.inspect(sample_repo, run=fake_run)
    assert info["identity"]["name"] == "test" and info["identity"]["source"]
    assert info["ssh"]["kind"] == "ssh" and info["ssh"]["host_alias"] == "github-personal"
    assert info["ssh"]["resolved_hostname"] == "github.com"
    loaded = [f for f in info["ssh"]["identity_files"] if f["exists"]]
    assert loaded and loaded[0]["fingerprint"].startswith("SHA256:") and info["ssh"]["agent"]["loaded"] is True
    assert info["signing"]["enabled"] is True and info["signing"]["key_id"] == "D519886F454D7B57" and info["signing"]["status"] == "ok"
    assert info["head"]["signature"] in c.SIGNATURE_WORDS.values()


def test_inspect_without_tools_degrades(sample_repo):
    def nothing(argv, cwd=None):
        if argv[0] == "git":
            return c.run_cmd(argv, cwd)
        return 127, ""
    info = c.inspect(sample_repo, run=nothing)
    assert info["ssh"]["kind"] == "none" and info["ssh"]["agent"]["running"] is False
    assert info["signing"]["status"] == "disabled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_credentials.py -q`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement inspection**

```python
# argonauta/web/credentials.py
"""Read-only view of the credentials git will use for this repository.

Reads: git config (with origins), `ssh -G` for the remote host, `.pub` files,
`ssh-add -l`, `gpg --list-keys --with-colons`, and git's own signature check.
Never opens a private key file or prints secret material.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from argonauta import gitops

Runner = Callable[[list[str], Path | None], tuple[int, str]]

SIGNATURE_WORDS = {
    "G": "good", "B": "bad", "U": "good_untrusted", "X": "expired_signature",
    "Y": "expired_key", "R": "revoked", "E": "unknown_key", "N": "unsigned",
}


def run_cmd(argv: list[str], cwd: Path | None = None, timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout + proc.stderr


def _expand(path: str) -> str:
    return os.path.expanduser(path)


# ----- parsers ------------------------------------------------------------------
_SCP_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//).+")


def remote_kind(url: str) -> tuple[str, str | None]:
    url = (url or "").strip()
    if not url:
        return "none", None
    if url.startswith("ssh://"):
        host = url[6:].split("/", 1)[0]
        host = host.split("@")[-1].split(":")[0]
        return "ssh", host
    if url.startswith(("http://", "https://")):
        host = url.split("//", 1)[1].split("/", 1)[0].split("@")[-1]
        return "https", host
    m = _SCP_RE.match(url)
    if m and not url.startswith(("/", ".", "~")):
        return "ssh", m.group(1)
    return "local", None


def parse_ssh_config(text: str) -> dict:
    out: dict = {"hostname": None, "user": None, "identity_files": []}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "hostname":
            out["hostname"] = value
        elif key == "user":
            out["user"] = value
        elif key == "identityfile":
            out["identity_files"].append(value)
    return out


def parse_ssh_add(text: str) -> list[str]:
    fps = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith(("SHA256:", "MD5:")):
            fps.append(parts[1])
    return fps


def parse_keygen(text: str) -> dict:
    parts = text.strip().split()
    if len(parts) < 3:
        return {}
    comment = " ".join(parts[2:-1]) if len(parts) > 3 else ""
    key_type = parts[-1].strip("()") if parts[-1].startswith("(") else ""
    return {"bits": int(parts[0]) if parts[0].isdigit() else 0, "fingerprint": parts[1], "comment": comment, "type": key_type}


def _ts(value: str) -> str | None:
    if not value or not value.isdigit():
        return None
    return datetime.fromtimestamp(int(value), UTC).date().isoformat()


def parse_gpg_colons(text: str) -> dict | None:
    key: dict | None = None
    for line in text.splitlines():
        f = line.split(":")
        if f[0] == "pub" and key is None:
            validity = f[1] if len(f) > 1 else ""
            status = {"e": "expired", "r": "revoked", "i": "invalid"}.get(validity, "ok")
            key = {"key_id": f[4], "fingerprint": None, "uid": None, "created": _ts(f[5]), "expires": _ts(f[6]) if len(f) > 6 else None, "status": status}
        elif f[0] == "fpr" and key and key["fingerprint"] is None:
            key["fingerprint"] = f[9] if len(f) > 9 else None
        elif f[0] == "uid" and key and key["uid"] is None:
            key["uid"] = f[9] if len(f) > 9 else None
    return key


def parse_config_origin(text: str) -> tuple[str | None, str | None]:
    line = text.strip().splitlines()[0] if text.strip() else ""
    if "\t" not in line:
        return None, None
    origin, value = line.split("\t", 1)
    source = origin.replace("file:", "", 1)
    home = os.path.expanduser("~")
    if source.startswith(home):
        source = "~" + source[len(home):]
    elif source.endswith(".git/config") or source == ".git/config":
        source = "repo .git/config"
    return value, source


# ----- inspection ------------------------------------------------------------------
def _git_config(run: Runner, repo_root: Path, key: str) -> tuple[str | None, str | None]:
    rc, out = run(["git", "config", "--show-origin", "--get", key], repo_root)
    return parse_config_origin(out) if rc == 0 else (None, None)


def _identity(run: Runner, repo_root: Path) -> dict:
    name, name_src = _git_config(run, repo_root, "user.name")
    email, email_src = _git_config(run, repo_root, "user.email")
    return {"name": name, "email": email, "source": name_src or email_src or "unset"}


def _ssh(run: Runner, repo_root: Path) -> dict:
    remotes = gitops.list_remotes(repo_root)
    url = remotes[0].url if remotes else ""
    kind, host = remote_kind(url)
    info: dict = {
        "remote_url": url or None, "kind": kind, "host_alias": host, "resolved_hostname": None, "user": None,
        "identity_files": [], "agent": {"running": False, "loaded": None}, "credential_helper": None,
    }
    if kind == "https":
        helper, _ = _git_config(run, repo_root, "credential.helper")
        info["credential_helper"] = helper or "none configured"
        return info
    if kind != "ssh":
        return info
    rc, out = run(["ssh", "-G", host], repo_root)
    cfg = parse_ssh_config(out) if rc == 0 else {"hostname": None, "user": None, "identity_files": []}
    info["resolved_hostname"] = cfg["hostname"]
    info["user"] = cfg["user"]
    rc, out = run(["ssh-add", "-l"], repo_root)
    agent_fps = parse_ssh_add(out) if rc in (0, 1) else []
    info["agent"]["running"] = rc in (0, 1)
    any_loaded = False
    for ident in cfg["identity_files"]:
        pub = _expand(ident) + ".pub"
        entry = {"path": ident, "exists": os.path.isfile(pub), "type": None, "bits": None, "fingerprint": None, "comment": None, "in_agent": None}
        if entry["exists"]:
            rc, out = run(["ssh-keygen", "-l", "-f", pub], repo_root)
            if rc == 0:
                entry.update(parse_keygen(out))
                entry["in_agent"] = entry["fingerprint"] in agent_fps
                any_loaded = any_loaded or bool(entry["in_agent"])
        info["identity_files"].append(entry)
    info["agent"]["loaded"] = any_loaded if info["agent"]["running"] else None
    return info


def _signing(run: Runner, repo_root: Path) -> dict:
    enabled_raw, _ = _git_config(run, repo_root, "commit.gpgsign")
    enabled = (enabled_raw or "").lower() in ("true", "1", "yes", "on")
    fmt, _ = _git_config(run, repo_root, "gpg.format")
    fmt = (fmt or "openpgp").lower()
    key, _ = _git_config(run, repo_root, "user.signingkey")
    info: dict = {"enabled": enabled, "format": fmt if (enabled or key) else None, "key_id": key, "fingerprint": None, "uid": None, "created": None, "expires": None, "status": "disabled"}
    if not enabled:
        return info
    if not key:
        info["status"] = "missing"
        return info
    if fmt == "ssh":
        pub = _expand(key)
        if os.path.isfile(pub):
            rc, out = run(["ssh-keygen", "-l", "-f", pub], repo_root)
            parsed = parse_keygen(out) if rc == 0 else {}
            info.update({"fingerprint": parsed.get("fingerprint"), "uid": parsed.get("comment"), "status": "ok" if parsed else "missing"})
        else:
            info["status"] = "missing"
        return info
    rc, out = run(["gpg", "--batch", "--list-keys", "--with-colons", key], repo_root)
    parsed = parse_gpg_colons(out) if rc == 0 else None
    if parsed is None:
        info["status"] = "missing"
        return info
    info.update({k: parsed[k] for k in ("key_id", "fingerprint", "uid", "created", "expires", "status")})
    return info


def _head(repo_root: Path) -> dict:
    sha = gitops.head_sha(repo_root)
    if not sha:
        return {"sha": None, "signature": "unsigned", "signer": None, "key_id": None}
    try:
        code, signer, key_id = gitops.signature_status(repo_root, "HEAD")
    except gitops.GitError:
        return {"sha": sha, "signature": "unsigned", "signer": None, "key_id": None}
    return {"sha": sha, "signature": SIGNATURE_WORDS.get(code, "unsigned"), "signer": signer or None, "key_id": key_id or None}


def inspect(repo_root: Path, run: Runner = run_cmd) -> dict:
    return {"identity": _identity(run, repo_root), "ssh": _ssh(run, repo_root), "signing": _signing(run, repo_root), "head": _head(repo_root)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_credentials.py -q`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add argonauta/web/credentials.py tests/test_credentials.py
git commit -m "Add read-only credential inspection for the web UI"
```

---

### Task 7: HTTP server core, graph lanes and read-only API

**Files:**
- Create: `argonauta/web/graph.py`
- Create: `argonauta/web/server.py`
- Create: `argonauta/web/api.py` (read routes; Tasks 8 and 9 append to it)
- Create: `argonauta/web/static/index.html` (placeholder page replaced in Task 11; must exist so `/` serves)
- Test: `tests/test_graph.py`, `tests/test_api.py` (fixture reused by Tasks 8 and 9)

**Interfaces:**
- Consumes: Task 1 gitops functions; Task 2 `Registry`, `Repo`; Task 3 `JobRunner`, `JobConflict`; Task 5 `TerminalManager`; existing `Store`, `load_settings`, `describe_provider`, `workflow.test_command_for`, `diffs._path_problem`.
- Produces:
  - `graph.assign_lanes(commits: list[GraphCommit]) -> list[dict]` (each dict: commit fields + `lane: int`, `parent_lanes: list[int]`, `merged_lanes: list[int]`, `active: list[bool]`)
  - `server.AppContext(registry, jobs, terminals, token, static_dir, port=0)`, `.allowed_hosts() -> set[str]`, `.allowed_origins() -> set[str]`
  - `server.create_server(ctx, host="127.0.0.1", port=0) -> ArgonautaServer` (subclass of `ThreadingHTTPServer` with `.ctx`)
  - `server.new_token() -> str`
  - `api.ApiError(status: int, message: str)`
  - `api.route(method, pattern)` decorator; `api.dispatch(ctx, method, path, query, body) -> tuple[int, dict]`
  - helpers used by later tasks: `api.resolve_repo(ctx, repo_id) -> Repo`, `api.open_store(repo) -> Store`, `api.record_event(repo, kind, message, ref_table=None, ref_id=None)`, `api.clean_paths(repo, value) -> list[str]`, `api.require_str(body, key, max_len, allow_empty=False) -> str`, `api.jsonable(obj)`
  - HTTP: `GET /`, static allowlist, `GET /api/workspaces`, `POST /api/workspaces`, `PATCH|DELETE /api/workspaces/{wid}`, `POST /api/workspaces/{wid}/repos`, `DELETE /api/workspaces/{wid}/repos/{rid}`, `GET /api/repos/{rid}/state`, `GET /api/repos/{rid}/graph`, `GET /api/repos/{rid}/commits/{sha}`, `GET /api/repos/{rid}/diff`, `GET /api/jobs/{jid}`

- [ ] **Step 1: Write the failing graph test**

```python
# tests/test_graph.py
from argonauta.gitops import GraphCommit
from argonauta.web.graph import assign_lanes


def c(sha, parents, subject=""):
    return GraphCommit(sha=sha, short=sha[:7], parents=parents, refs=[], author="a", email="e", date="d", subject=subject or sha, signature="N")


def test_linear_history_stays_in_lane_zero():
    rows = assign_lanes([c("c3", ["c2"]), c("c2", ["c1"]), c("c1", [])])
    assert [r["lane"] for r in rows] == [0, 0, 0]
    assert rows[0]["parent_lanes"] == [0] and rows[-1]["active"] == []


def test_branch_and_merge_use_two_lanes():
    # M merges A (first parent) and B; both come from C.
    rows = assign_lanes([c("M", ["A", "B"]), c("A", ["C"]), c("B", ["C"]), c("C", [])])
    by = {r["sha"]: r for r in rows}
    assert by["M"]["lane"] == 0 and by["M"]["parent_lanes"] == [0, 1]
    assert by["A"]["lane"] == 0 and by["B"]["lane"] == 1
    assert by["C"]["lane"] == 0 and by["C"]["merged_lanes"] == [1]
    assert by["A"]["active"] == [True, True] and by["C"]["active"] == []


def test_free_columns_are_reused():
    rows = assign_lanes([c("X", ["P"]), c("Y", ["Q"]), c("P", []), c("Q", []), c("Z", [])])
    assert [r["lane"] for r in rows] == [0, 1, 0, 1, 0]
```

- [ ] **Step 2: Implement lanes**

```python
# argonauta/web/graph.py
"""Lane assignment for the commit graph. Pure function, no git access."""

from __future__ import annotations

from dataclasses import asdict

from argonauta.gitops import GraphCommit


def assign_lanes(commits: list[GraphCommit]) -> list[dict]:
    columns: list[str | None] = []  # sha each column is waiting for
    rows: list[dict] = []

    def free_column() -> int:
        for i, s in enumerate(columns):
            if s is None:
                return i
        columns.append(None)
        return len(columns) - 1

    for commit in commits:
        waiting = [i for i, s in enumerate(columns) if s == commit.sha]
        if waiting:
            lane, merged = waiting[0], waiting[1:]
            for i in merged:
                columns[i] = None
        else:
            lane, merged = free_column(), []
        parent_lanes: list[int] = []
        if commit.parents:
            columns[lane] = commit.parents[0]
            parent_lanes.append(lane)
            for parent in commit.parents[1:]:
                existing = next((i for i, s in enumerate(columns) if s == parent), None)
                if existing is None:
                    existing = free_column()
                    columns[existing] = parent
                parent_lanes.append(existing)
        else:
            columns[lane] = None
        while columns and columns[-1] is None:
            columns.pop()
        row = asdict(commit)
        row.update(lane=lane, parent_lanes=parent_lanes, merged_lanes=merged, active=[s is not None for s in columns])
        rows.append(row)
    return rows
```

Run: `.venv/bin/python -m pytest tests/test_graph.py -q` → 3 PASS.

- [ ] **Step 3: Write the failing API tests and fixture**

```python
# tests/test_api.py
"""API tests against a real server thread, a temp repo and a bare remote."""
import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from argonauta.web.jobs import JobRunner
from argonauta.web.server import AppContext, create_server, new_token
from argonauta.web.terminal import TerminalManager
from argonauta.workspaces import open_registry
from tests.conftest import SAMPLE_PROJECT, SAMPLE_REPLY, git


class Client:
    def __init__(self, port: int, token: str):
        self.port, self.token = port, token

    def request(self, method, path, body=None, token="default", origin="default", host="default", raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {}
        if token == "default":
            token = self.token
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin == "default":
            origin = f"http://127.0.0.1:{self.port}"
        if origin is not None:
            headers["Origin"] = origin
        if host != "default":
            headers["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        if raw:
            return resp.status, payload, dict(resp.getheaders())
        try:
            return resp.status, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return resp.status, payload.decode(errors="replace")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body or {}, **kw)

    def wait_job(self, job_id, timeout=120):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, job = self.get(f"/api/jobs/{job_id}")
            assert status == 200
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.1)
        raise AssertionError("job did not finish")


@pytest.fixture
def api(tmp_path, sample_repo, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "scripted")
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(SAMPLE_REPLY))
    registry = open_registry()
    ws = registry.add_workspace("Test")
    repo = registry.add_repo(ws.id, sample_repo)
    ctx = AppContext(registry=registry, jobs=JobRunner(), terminals=TerminalManager(), token=new_token(),
                     static_dir=Path(__file__).resolve().parent.parent / "argonauta" / "web" / "static")
    server = create_server(ctx, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(ctx.port, ctx.token)
    client.repo = repo
    client.workspace = ws
    client.ctx = ctx
    yield client
    server.shutdown()
    ctx.terminals.shutdown()
    server.server_close()


# ----- auth and static ---------------------------------------------------------
def test_index_and_static_served_without_token(api):
    status, body, headers = api.get("/", token=None, origin=None, raw=True)
    assert status == 200 and b"<!doctype html" in body.lower()
    assert "no-store" in headers.get("Cache-Control", "")
    assert api.get("/app.js", token=None, origin=None, raw=True)[0] in (200, 404)  # exists after Task 11
    assert api.get("/../pyproject.toml", token=None, origin=None, raw=True)[0] == 404
    assert api.get("/static/../../pyproject.toml", token=None, origin=None, raw=True)[0] == 404


def test_api_requires_token_origin_and_host(api):
    assert api.get("/api/workspaces")[0] == 200
    assert api.get("/api/workspaces", token=None)[0] == 401
    assert api.get("/api/workspaces", token="wrong")[0] == 401
    assert api.get("/api/workspaces", origin=None)[0] == 403
    assert api.get("/api/workspaces", host="evil.example")[0] == 403


def test_foreign_origin_rejected(api):
    status, body = api.post(f"/api/workspaces", {"name": "x"}, origin="http://evil.example")
    assert status == 403
    assert all(w["name"] != "x" for w in api.get("/api/workspaces")[1]["workspaces"])


def test_bad_json_and_oversized_bodies(api):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    conn.request("POST", "/api/workspaces", body=b"{nope", headers={"Authorization": f"Bearer {api.token}", "Origin": f"http://127.0.0.1:{api.port}", "Content-Type": "application/json"})
    assert conn.getresponse().status == 400
    conn.close()
    big = {"name": "x" * (1024 * 1024 + 10)}
    assert api.post("/api/workspaces", big)[0] == 413


# ----- workspaces -------------------------------------------------------------
def test_workspace_crud(api, tmp_path):
    status, data = api.get("/api/workspaces")
    assert status == 200 and data["workspaces"][0]["repos"][0]["id"] == api.repo.id
    status, ws = api.post("/api/workspaces", {"name": "Second"})
    assert status == 201 and ws["name"] == "Second"
    assert api.request("PATCH", f"/api/workspaces/{ws['id']}", {"name": "Renamed"})[1]["name"] == "Renamed"
    assert api.post(f"/api/workspaces/{ws['id']}/repos", {"path": str(tmp_path)})[0] == 400
    other = tmp_path / "other"
    shutil.copytree(SAMPLE_PROJECT, other)
    git(other, "init", "-q")
    status, repo = api.post(f"/api/workspaces/{ws['id']}/repos", {"path": str(other)})
    assert status == 201 and repo["exists"] is True and repo["initialized"] is False
    assert api.request("DELETE", f"/api/workspaces/{ws['id']}/repos/{repo['id']}")[0] == 200
    assert api.request("DELETE", f"/api/workspaces/{ws['id']}")[0] == 200
    assert api.request("DELETE", f"/api/workspaces/{ws['id']}")[0] == 404
    assert api.post("/api/workspaces", {"name": ""})[0] == 400


# ----- read endpoints ----------------------------------------------------------
def test_state_graph_diff_and_commit(api):
    repo = api.repo
    (repo.path / "README.md").write_text("staged\n")
    git(repo.path, "add", "README.md")
    (repo.path / "calc" / "core.py").write_text("changed\n")
    (repo.path / "new.txt").write_text("new\n")
    status, state = api.get(f"/api/repos/{repo.id}/state")
    assert status == 200
    assert state["branch"] in ("main", "master") and state["head"]
    assert [e["path"] for e in state["status"]["staged"]] == ["README.md"]
    assert [e["path"] for e in state["status"]["unstaged"]] == ["calc/core.py"]
    assert [e["path"] for e in state["status"]["untracked"]] == ["new.txt"]
    assert state["store"]["initialized"] is False and state["running_job"] is None
    assert any(b["is_current"] for b in state["branches"])

    status, graph = api.get(f"/api/repos/{repo.id}/graph?limit=10")
    assert status == 200 and graph["commits"][0]["lane"] == 0 and graph["commits"][0]["subject"] == "sample project"
    sha = graph["commits"][0]["sha"]
    status, detail = api.get(f"/api/repos/{repo.id}/commits/{sha}")
    assert status == 200 and detail["commit"]["sha"] == sha and any(f["path"] == "calc/core.py" for f in detail["files"])
    assert api.get(f"/api/repos/{repo.id}/commits/notasha")[0] == 400
    assert api.get(f"/api/repos/{repo.id}/commits/{'0' * 40}")[0] == 404

    assert "+new" in api.get(f"/api/repos/{repo.id}/diff?path=new.txt&area=worktree")[1]["diff"]
    assert "+staged" in api.get(f"/api/repos/{repo.id}/diff?path=README.md&area=index")[1]["diff"]
    assert api.get(f"/api/repos/{repo.id}/diff?path=../x&area=worktree")[0] == 400
    assert api.get(f"/api/repos/{repo.id}/diff?path=.env&area=worktree")[0] == 400
    assert api.get(f"/api/repos/{repo.id}/diff?path=new.txt&area=nope")[0] == 400


def test_unknown_or_missing_repo_is_404(api, tmp_path):
    assert api.get("/api/repos/r_deadbeef/state")[0] == 404
    assert api.get("/api/repos/evil/state")[0] == 404
    other = tmp_path / "gone"
    shutil.copytree(SAMPLE_PROJECT, other)
    git(other, "init", "-q")
    status, repo = api.post(f"/api/workspaces/{api.workspace.id}/repos", {"path": str(other)})
    shutil.rmtree(other)
    assert api.get(f"/api/repos/{repo['id']}/state")[0] == 404
    assert any(r["exists"] is False for r in api.get("/api/workspaces")[1]["workspaces"][0]["repos"])


def test_unknown_job_is_404(api):
    assert api.get("/api/jobs/j_nope")[0] == 404
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'argonauta.web.server'`.

- [ ] **Step 5: Write the placeholder page**

```html
<!-- argonauta/web/static/index.html (replaced in Task 11) -->
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Argonauta</title></head>
<body><main><h1>Argonauta</h1><p>UI loading…</p></main></body></html>
```

- [ ] **Step 6: Implement `api.py` base and read routes**

```python
# argonauta/web/api.py
"""JSON API. Each route is a function (ctx, params, query, body) -> (status, payload).

All input is untrusted: ids are matched by regex, paths go through the diff
path rules, strings are length-limited, and repositories are only ever
resolved through the registry.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from argonauta import gitops
from argonauta.config import Settings, load_settings
from argonauta.db import Store, StoreError
from argonauta.diffs import _path_problem
from argonauta.logging_setup import get_logger
from argonauta.providers import describe_provider
from argonauta.web.graph import assign_lanes
from argonauta.workspaces import Registry, RegistryError, Repo

log = get_logger()
Handler = Callable[[Any, dict, dict, dict], tuple[int, dict]]
ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
ID = r"[A-Za-z0-9_]{3,40}"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    compiled = re.compile(pattern)

    def register(fn: Handler) -> Handler:
        ROUTES.append((method, compiled, fn))
        return fn

    return register


def dispatch(ctx: Any, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
    path_matched = False
    for route_method, pattern, fn in ROUTES:
        m = pattern.match(path)
        if not m:
            continue
        path_matched = True
        if route_method != method:
            continue
        return fn(ctx, m.groupdict(), query, body)
    raise ApiError(405 if path_matched else 404, "method not allowed" if path_matched else "not found")


# ----- helpers --------------------------------------------------------------------
def jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def require_str(body: dict, key: str, max_len: int, allow_empty: bool = False) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    stripped = value.strip()
    if not stripped and not allow_empty:
        raise ApiError(400, f"{key} must not be empty")
    if len(value) > max_len:
        raise ApiError(400, f"{key} must be at most {max_len} characters")
    return value if allow_empty else stripped


def require_bool(body: dict, key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(400, f"{key} must be true or false")
    return value


def query_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, f"{key} must be an integer") from exc
    return max(lo, min(value, hi))


def resolve_repo(ctx: Any, repo_id: str) -> Repo:
    repo = ctx.registry.get_repo(repo_id)
    if repo is None or not repo.exists:
        raise ApiError(404, "repository not found")
    return repo


def repo_settings(repo: Repo) -> Settings:
    return load_settings(repo.path)


def open_store(repo: Repo) -> Store:
    if not repo.initialized:
        raise ApiError(409, "repository is not initialised; run init first")
    try:
        return Store.open(repo.path / ".argonauta" / "argonauta.db")
    except StoreError as exc:
        raise ApiError(500, str(exc)) from exc


def record_event(repo: Repo, kind: str, message: str, ref_table: str | None = None, ref_id: int | None = None) -> None:
    if not repo.initialized:
        return
    store = Store.open(repo.path / ".argonauta" / "argonauta.db")
    try:
        store.record_event(kind, message, ref_table, ref_id)
    finally:
        store.close()


def clean_paths(repo: Repo, value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 500:
        raise ApiError(400, "paths must be a non-empty list of at most 500 strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 4096:
            raise ApiError(400, "paths must be strings")
        problem = _path_problem(item, repo.path)
        if problem:
            raise ApiError(400, f"invalid path: {problem}")
        out.append(item)
    return out


def _git(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except gitops.GitError as exc:
        raise ApiError(409, str(exc)) from exc


# ----- workspaces -------------------------------------------------------------------
@route("GET", r"^/api/workspaces$")
def list_workspaces(ctx, params, query, body):
    return 200, ctx.registry.to_dict()


@route("POST", r"^/api/workspaces$")
def create_workspace(ctx, params, query, body):
    try:
        ws = ctx.registry.add_workspace(require_str(body, "name", 100))
    except RegistryError as exc:
        raise ApiError(400, str(exc)) from exc
    return 201, ws.to_dict()


@route("PATCH", rf"^/api/workspaces/(?P<wid>{ID})$")
def rename_workspace(ctx, params, query, body):
    try:
        ws = ctx.registry.rename_workspace(params["wid"], require_str(body, "name", 100))
    except RegistryError as exc:
        raise ApiError(404 if "no workspace" in str(exc) else 400, str(exc)) from exc
    return 200, ws.to_dict()


@route("DELETE", rf"^/api/workspaces/(?P<wid>{ID})$")
def delete_workspace(ctx, params, query, body):
    try:
        ctx.registry.remove_workspace(params["wid"])
    except RegistryError as exc:
        raise ApiError(404, str(exc)) from exc
    return 200, {"ok": True}


@route("POST", rf"^/api/workspaces/(?P<wid>{ID})/repos$")
def add_repo(ctx, params, query, body):
    path = require_str(body, "path", 4096)
    try:
        repo = ctx.registry.add_repo(params["wid"], Path(path))
    except RegistryError as exc:
        raise ApiError(404 if "no workspace" in str(exc) else 400, str(exc)) from exc
    return 201, repo.to_dict()


@route("DELETE", rf"^/api/workspaces/(?P<wid>{ID})/repos/(?P<rid>{ID})$")
def remove_repo(ctx, params, query, body):
    try:
        ctx.registry.remove_repo(params["rid"])
    except RegistryError as exc:
        raise ApiError(404, str(exc)) from exc
    return 200, {"ok": True}


# ----- repository reads -------------------------------------------------------------
def _status_areas(entries: list[gitops.FileStatus]) -> dict:
    areas: dict[str, list[dict]] = {"staged": [], "unstaged": [], "untracked": [], "conflicted": []}
    for e in entries:
        d = jsonable(e)
        if e.conflicted:
            areas["conflicted"].append(d)
        elif e.untracked:
            areas["untracked"].append(d)
        else:
            if e.index not in " ?":
                areas["staged"].append(d)
            if e.worktree not in " ?":
                areas["unstaged"].append(d)
    return areas


def _store_summary(repo: Repo) -> dict:
    from argonauta.workflow import test_command_for

    settings = repo_settings(repo)
    summary = {"initialized": repo.initialized, "indexed_files": None, "last_indexed_at": None, "test_command": None, "provider": describe_provider(settings), "provider_name": settings.effective_provider}
    if repo.initialized:
        store = open_store(repo)
        try:
            summary.update(indexed_files=store.file_count(), last_indexed_at=store.last_indexed_at(), test_command=test_command_for(settings, store))
        finally:
            store.close()
    return summary


@route("GET", rf"^/api/repos/(?P<rid>{ID})/state$")
def repo_state(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    root = repo.path
    branches = _git(lambda: gitops.list_branches(root))
    current = next((b for b in branches if b.is_current), None)
    ab = gitops.ahead_behind(root)
    running = ctx.jobs.running_for(repo.id)
    return 200, {
        "repo": repo.to_dict(),
        "branch": gitops.current_branch(root),
        "head": gitops.head_sha(root),
        "upstream": current.upstream if current else None,
        "ahead": ab[0] if ab else None,
        "behind": ab[1] if ab else None,
        "remotes": jsonable(_git(lambda: gitops.list_remotes(root))),
        "branches": jsonable(branches),
        "status": _status_areas(_git(lambda: gitops.status_detail(root))),
        "store": _store_summary(repo),
        "running_job": running.to_dict() if running else None,
        "terminals": [t.to_dict() for t in ctx.terminals.list_for(root)],
    }


@route("GET", rf"^/api/repos/(?P<rid>{ID})/graph$")
def repo_graph(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    limit = query_int(query, "limit", 200, 1, 2000)
    commits = _git(lambda: gitops.graph_log(repo.path, limit))
    return 200, {"commits": assign_lanes(commits), "limit": limit}


@route("GET", rf"^/api/repos/(?P<rid>{ID})/commits/(?P<sha>[^/]+)$")
def repo_commit(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    sha = params["sha"]
    if not SHA_RE.match(sha):
        raise ApiError(400, "sha must be 7 to 40 hex characters")
    if not gitops.commit_exists(repo.path, sha):
        raise ApiError(404, "commit not found")
    detail = _git(lambda: gitops.commit_detail(repo.path, sha))
    return 200, jsonable(detail)


@route("GET", rf"^/api/repos/(?P<rid>{ID})/diff$")
def repo_diff(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    path = query.get("path", [""])[0]
    area = query.get("area", ["worktree"])[0]
    if area not in ("worktree", "index"):
        raise ApiError(400, "area must be worktree or index")
    problem = _path_problem(path, repo.path)
    if problem:
        raise ApiError(400, f"invalid path: {problem}")
    return 200, {"path": path, "area": area, "diff": _git(lambda: gitops.diff_file(repo.path, path, area))}


@route("GET", rf"^/api/jobs/(?P<jid>{ID})$")
def get_job(ctx, params, query, body):
    job = ctx.jobs.get(params["jid"])
    if job is None:
        raise ApiError(404, "job not found")
    return 200, job.to_dict()
```

- [ ] **Step 7: Implement `server.py`**

```python
# argonauta/web/server.py
"""Loopback-only HTTP server for the web UI.

Static page + JSON API + WebSocket terminals. Every API call needs the
per-run bearer token, a loopback Host and a matching Origin.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from argonauta.logging_setup import get_logger
from argonauta.web import api
from argonauta.web.jobs import JobRunner
from argonauta.web.terminal import TerminalManager
from argonauta.workspaces import Registry

log = get_logger()
MAX_BODY = 1024 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/vendor/xterm.js": ("vendor/xterm.js", "text/javascript; charset=utf-8"),
    "/vendor/xterm.css": ("vendor/xterm.css", "text/css; charset=utf-8"),
    "/vendor/addon-fit.js": ("vendor/addon-fit.js", "text/javascript; charset=utf-8"),
}
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'"


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class AppContext:
    registry: Registry
    jobs: JobRunner
    terminals: TerminalManager
    token: str
    static_dir: Path
    port: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts()}


class ArgonautaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], ctx: AppContext):
        super().__init__(address, RequestHandler)
        self.ctx = ctx
        ctx.port = self.server_address[1]


def create_server(ctx: AppContext, host: str = "127.0.0.1", port: int = 0) -> ArgonautaServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the UI only listens on 127.0.0.1 or localhost")
    return ArgonautaServer(("127.0.0.1", port), ctx)


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ArgonautaServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - never log requests
        return

    @property
    def ctx(self) -> AppContext:
        return self.server.ctx

    # ----- plumbing -----
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.ctx.allowed_hosts()

    def _origin_ok(self) -> bool:
        return self.headers.get("Origin", "") in self.ctx.allowed_origins()

    def _token_ok(self, token: str | None = None) -> bool:
        if token is None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            token = auth[7:].strip()
        return hmac.compare_digest(token.encode(), self.ctx.token.encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise api.ApiError(413, "request body too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise api.ApiError(400, "body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise api.ApiError(400, "body must be a JSON object")
        return body

    def _drain_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)

    # ----- routing -----
    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if not self._host_ok():
            self._drain_body()
            self._send_json(403, {"error": "forbidden host"})
            return
        if path.startswith("/ws/"):
            self._handle_websocket(path)
            return
        if path.startswith("/api/"):
            self._handle_api(method, path, parse_qs(parts.query))
            return
        if method == "GET":
            self._serve_static(path)
            return
        self._drain_body()
        self._send_json(404, {"error": "not found"})

    def _serve_static(self, path: str) -> None:
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._send_json(404, {"error": "not found"})
            return
        name, content_type = entry
        file_path = self.ctx.static_dir / name
        if not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _handle_api(self, method: str, path: str, query: dict) -> None:
        try:
            if not self._token_ok():
                self._drain_body()
                raise api.ApiError(401, "missing or invalid token")
            if not self._origin_ok():
                self._drain_body()
                raise api.ApiError(403, "forbidden origin")
            body = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
            status, payload = api.dispatch(self.ctx, method, path, query, body)
            self._send_json(status, payload)
        except api.ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the page
            log.exception("api %s %s failed", method, path)
            self._send_json(500, {"error": f"internal error: {exc.__class__.__name__}"})

    def _handle_websocket(self, path: str) -> None:
        # Filled in by Task 9.
        self._send_json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")
```

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_graph.py -q`
Expected: all PASS. Known pitfall: `http.client` sends `Host: 127.0.0.1:<port>` automatically, which is why the fixture never sets it unless the test overrides it.

- [ ] **Step 9: Commit**

```bash
git add argonauta/web/graph.py argonauta/web/server.py argonauta/web/api.py argonauta/web/static/index.html tests/test_graph.py tests/test_api.py
git commit -m "Add loopback HTTP server, graph lanes and read-only API"
```

---

### Task 8: Git write actions and remote jobs

**Files:**
- Modify: `argonauta/web/api.py` (append routes)
- Test: `tests/test_api.py` (append tests)

**Interfaces:**
- Consumes: Task 7 helpers; Task 1 `stage`, `unstage`, `discard`, `commit_message`, `valid_branch_name`, `create_branch`, `checkout`, `delete_branch`, `fetch`, `pull`, `push`; Task 3 `JobConflict`.
- Produces HTTP: `POST /api/repos/{rid}/stage|unstage|discard` `{paths}`; `POST /api/repos/{rid}/commit` `{message, amend}` → `{sha}`; `POST /api/repos/{rid}/branches` `{name, from?}`; `POST /api/repos/{rid}/checkout` `{name}`; `DELETE /api/repos/{rid}/branches/{name}?force=1`; `POST /api/repos/{rid}/jobs/fetch|pull|push` `{set_upstream?}` → 202 `{job}`.
- Produces helper: `api.submit_job(ctx, repo, kind, fn) -> tuple[int, dict]` (202 with job dict, 409 on conflict).

- [ ] **Step 1: Append the failing tests**

```python
# tests/test_api.py (append)
def test_stage_unstage_discard_commit(api):
    repo = api.repo
    (repo.path / "README.md").write_text("edited\n")
    (repo.path / "junk.txt").write_text("junk\n")
    assert api.post(f"/api/repos/{repo.id}/stage", {"paths": ["README.md"]})[0] == 200
    assert [e["path"] for e in api.get(f"/api/repos/{repo.id}/state")[1]["status"]["staged"]] == ["README.md"]
    assert api.post(f"/api/repos/{repo.id}/unstage", {"paths": ["README.md"]})[0] == 200
    assert api.post(f"/api/repos/{repo.id}/stage", {"paths": ["../etc"]})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/stage", {"paths": []})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/discard", {"paths": ["junk.txt"]})[0] == 200
    assert not (repo.path / "junk.txt").exists()
    api.post(f"/api/repos/{repo.id}/stage", {"paths": ["README.md"]})
    status, data = api.post(f"/api/repos/{repo.id}/commit", {"message": "from the ui"})
    assert status == 201 and len(data["sha"]) == 40
    assert git(repo.path, "log", "-1", "--format=%s").strip() == "from the ui"
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": "nothing staged"})[0] == 409


def test_commit_rejects_bad_message(api):
    repo = api.repo
    (repo.path / "README.md").write_text("edited\n")
    api.post(f"/api/repos/{repo.id}/stage", {"paths": ["README.md"]})
    head = git(repo.path, "rev-parse", "HEAD")
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": "   "})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": "x" * 5001})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": 5})[0] == 400
    assert git(repo.path, "rev-parse", "HEAD") == head


def test_branch_name_validated_and_lifecycle(api):
    repo = api.repo
    assert api.post(f"/api/repos/{repo.id}/branches", {"name": "bad name"})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/branches", {"name": "a..b"})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/branches", {"name": "feat/ui"})[0] == 201
    assert api.post(f"/api/repos/{repo.id}/checkout", {"name": "feat/ui"})[0] == 200
    assert api.get(f"/api/repos/{repo.id}/state")[1]["branch"] == "feat/ui"
    assert api.request("DELETE", f"/api/repos/{repo.id}/branches/feat%2Fui")[0] == 409  # current branch
    main = [b["name"] for b in api.get(f"/api/repos/{repo.id}/state")[1]["branches"] if b["name"] in ("main", "master")][0]
    api.post(f"/api/repos/{repo.id}/checkout", {"name": main})
    assert api.request("DELETE", f"/api/repos/{repo.id}/branches/feat%2Fui")[0] == 200
    assert api.post(f"/api/repos/{repo.id}/checkout", {"name": "nope"})[0] == 409


def test_fetch_pull_push_jobs(api, tmp_path):
    repo = api.repo
    bare = tmp_path / "remote.git"
    git(repo.path, "init", "--bare", "-q", str(bare))
    git(repo.path, "remote", "add", "origin", str(bare))
    status, data = api.post(f"/api/repos/{repo.id}/jobs/push", {"set_upstream": True})
    assert status == 202
    job = api.wait_job(data["job"]["id"])
    assert job["status"] == "done", job
    for kind in ("fetch", "pull"):
        status, data = api.post(f"/api/repos/{repo.id}/jobs/{kind}")
        assert status == 202 and api.wait_job(data["job"]["id"])["status"] == "done"
    state = api.get(f"/api/repos/{repo.id}/state")[1]
    assert state["ahead"] == 0 and state["upstream"].startswith("origin/")


def test_push_without_credentials_fails_fast(api, monkeypatch):
    repo = api.repo
    git(repo.path, "remote", "add", "origin", "ssh://git@127.0.0.1:9/none.git")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=2")
    status, data = api.post(f"/api/repos/{repo.id}/jobs/push", {"set_upstream": True})
    assert status == 202
    job = api.wait_job(data["job"]["id"], timeout=60)
    assert job["status"] == "failed" and job["error"]


def test_second_job_for_same_repo_is_409(api, tmp_path):
    repo = api.repo
    bare = tmp_path / "remote.git"
    git(repo.path, "init", "--bare", "-q", str(bare))
    git(repo.path, "remote", "add", "origin", str(bare))
    import threading
    gate = threading.Event()
    original = api.ctx.jobs.submit
    def slow_submit(kind, rid, fn):
        return original(kind, rid, lambda p: (gate.wait(5), fn(p))[1])
    monkeypatch_submit = slow_submit
    api.ctx.jobs.submit = monkeypatch_submit
    first = api.post(f"/api/repos/{repo.id}/jobs/fetch")
    second = api.post(f"/api/repos/{repo.id}/jobs/fetch")
    gate.set()
    assert first[0] == 202 and second[0] == 409 and second[1]["job"]["id"] == first[1]["job"]["id"]
    api.wait_job(first[1]["job"]["id"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py -q -k "stage or commit_rejects or branch_name or jobs or credentials_fails or second_job"`
Expected: FAIL with 404s.

- [ ] **Step 3: Append the routes**

```python
# argonauta/web/api.py (append)
# ----- git actions ---------------------------------------------------------------------
from argonauta.web.jobs import JobConflict  # noqa: E402


def submit_job(ctx: Any, repo: Repo, kind: str, fn: Callable[[Callable[[str], None]], dict]) -> tuple[int, dict]:
    try:
        job = ctx.jobs.submit(kind, repo.id, fn)
    except JobConflict as exc:
        return 409, {"error": str(exc), "job": exc.job.to_dict()}
    log.info("job %s %s submitted for %s", job.id, kind, repo.id)
    return 202, {"job": job.to_dict()}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/stage$")
def stage_paths(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    paths = clean_paths(repo, body.get("paths"))
    _git(lambda: gitops.stage(repo.path, paths))
    record_event(repo, "git.stage", f"staged {len(paths)} paths")
    return 200, {"ok": True, "count": len(paths)}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/unstage$")
def unstage_paths(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    paths = clean_paths(repo, body.get("paths"))
    _git(lambda: gitops.unstage(repo.path, paths))
    record_event(repo, "git.unstage", f"unstaged {len(paths)} paths")
    return 200, {"ok": True, "count": len(paths)}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/discard$")
def discard_paths(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    paths = clean_paths(repo, body.get("paths"))
    _git(lambda: gitops.discard(repo.path, paths))
    record_event(repo, "git.discard", f"discarded changes in {len(paths)} paths")
    return 200, {"ok": True, "count": len(paths)}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/commit$")
def commit_staged(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    message = require_str(body, "message", 5000)
    amend = require_bool(body, "amend", False)
    staged = [e for e in _git(lambda: gitops.status_detail(repo.path)) if e.index not in " ?" and not e.conflicted]
    if not staged and not amend:
        raise ApiError(409, "nothing is staged")
    sha = _git(lambda: gitops.commit_message(repo.path, message, amend=amend))
    record_event(repo, "git.commit", f"committed {len(staged)} files as {sha[:10]}" + (" (amend)" if amend else ""))
    return 201, {"sha": sha}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/branches$")
def create_branch(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    name = require_str(body, "name", 200)
    start = require_str(body, "from", 200, allow_empty=True).strip() or None
    if not gitops.valid_branch_name(repo.path, name):
        raise ApiError(400, "name is not a valid branch name")
    if start and not (gitops.valid_branch_name(repo.path, start) or SHA_RE.match(start)):
        raise ApiError(400, "from must be a branch name or commit sha")
    _git(lambda: gitops.create_branch(repo.path, name, start))
    record_event(repo, "git.branch", f"created branch {name}")
    return 201, {"name": name}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/checkout$")
def checkout_branch(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    name = require_str(body, "name", 200)
    if not gitops.valid_branch_name(repo.path, name):
        raise ApiError(400, "name is not a valid branch name")
    _git(lambda: gitops.checkout(repo.path, name))
    record_event(repo, "git.checkout", f"checked out {name}")
    return 200, {"branch": gitops.current_branch(repo.path)}


@route("DELETE", rf"^/api/repos/(?P<rid>{ID})/branches/(?P<name>.+)$")
def delete_branch(ctx, params, query, body):
    from urllib.parse import unquote

    repo = resolve_repo(ctx, params["rid"])
    name = unquote(params["name"])
    if not gitops.valid_branch_name(repo.path, name):
        raise ApiError(400, "name is not a valid branch name")
    if name == gitops.current_branch(repo.path):
        raise ApiError(409, "cannot delete the current branch")
    force = query.get("force", ["0"])[0] == "1"
    _git(lambda: gitops.delete_branch(repo.path, name, force=force))
    record_event(repo, "git.branch.delete", f"deleted branch {name}")
    return 200, {"ok": True}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/jobs/(?P<kind>fetch|pull|push)$")
def remote_job(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    kind = params["kind"]
    set_upstream = require_bool(body, "set_upstream", False)

    def run(progress):
        progress(f"git {kind}")
        if kind == "fetch":
            out = gitops.fetch(repo.path)
        elif kind == "pull":
            out = gitops.pull(repo.path)
        else:
            out = gitops.push(repo.path, set_upstream=set_upstream)
        record_event(repo, f"git.{kind}", f"{kind} completed")
        return {"output": out[-4000:]}

    return submit_job(ctx, repo, kind, run)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: all PASS. `test_push_without_credentials_fails_fast` must finish in under 15 s; if it takes longer, `GIT_SSH_COMMAND` is not reaching the child (check `run_git` merges `os.environ`).

- [ ] **Step 5: Commit**

```bash
git add argonauta/web/api.py tests/test_api.py
git commit -m "Add staging, commit, branch and remote endpoints"
```

---

### Task 9: Assistant, settings, credentials, terminals and the WebSocket bridge

**Files:**
- Modify: `argonauta/web/api.py` (append routes)
- Modify: `argonauta/web/server.py` (replace `_handle_websocket`)
- Test: `tests/test_api.py` (append), `tests/test_ws_terminal.py`

**Interfaces:**
- Consumes: `workflow.init_repo, propose, apply_proposal, run_tests_now, commit_change, reject_proposal, revert_proposal, test_command_for, TEST_COMMAND_KEY, WorkflowError, ValidationError`; `indexer.index_repo`; `providers.ProviderUnavailable`; `runner.parse_command, RunnerError`; Task 6 `credentials.inspect`; Task 5 `TerminalManager`; Task 4 `WebSocketConnection`, `accept_key`, `ProtocolError`, `ConnectionClosed`, `OP_TEXT`, `OP_BINARY`.
- Produces HTTP: `POST .../init`, `GET .../proposals`, `GET .../proposals/{pid}`, `POST .../proposals/{pid}/reject|revert|commit`, `GET .../events`, `GET|PUT .../settings`, `POST .../jobs/index|propose|test|apply`, `GET .../credentials`, `POST .../terminals` → 201 `{terminal}`, `GET .../terminals`, `DELETE /api/terminals/{tid}`; `WS /ws/terminals/{tid}` (auth message first).

- [ ] **Step 1: Append failing API tests**

```python
# tests/test_api.py (append)
def test_assistant_loop_over_api(api):
    repo = api.repo
    r = f"/api/repos/{repo.id}"
    assert api.get(f"{r}/proposals")[0] == 409  # not initialised
    import sys
    status, data = api.post(f"{r}/init", {"test_command": f"{sys.executable} -m pytest -q"})
    assert status == 201 and data["initialized"] is True
    assert api.get(f"{r}/settings")[1]["test_command"].endswith("pytest -q")
    assert api.request("PUT", f"{r}/settings", {"test_command": ""})[0] == 400

    status, data = api.post(f"{r}/jobs/index")
    assert status == 202 and api.wait_job(data["job"]["id"])["result"]["file_count"] >= 4

    status, data = api.post(f"{r}/jobs/propose", {"task": "Make power() handle negative exponents", "dry_run": True})
    job = api.wait_job(data["job"]["id"])
    assert job["status"] == "done" and "calc/core.py" in job["result"]["context"]

    assert api.post(f"{r}/jobs/propose", {"task": "   "})[0] == 400
    status, data = api.post(f"{r}/jobs/propose", {"task": "Make power() handle negative exponents"})
    job = api.wait_job(data["job"]["id"])
    assert job["status"] == "done" and job["result"]["proposal"]["status"] == "proposed"
    pid = job["result"]["proposal"]["id"]
    assert api.get(f"{r}/proposals/{pid}")[1]["proposal"]["id"] == pid
    assert len(api.get(f"{r}/proposals")[1]["proposals"]) == 1

    status, data = api.post(f"{r}/jobs/apply", {"proposal_id": pid, "test": True, "commit": True})
    job = api.wait_job(data["job"]["id"], timeout=180)
    assert job["status"] == "done", job
    assert job["result"]["test_run"]["status"] == "passed" and job["result"]["commit_sha"]
    assert api.get(f"{r}/proposals/{pid}")[1]["proposal"]["status"] == "applied"
    assert api.post(f"{r}/proposals/{pid}/revert")[0] == 200
    assert api.get(f"{r}/proposals/{pid}")[1]["proposal"]["status"] == "reverted"
    kinds = [e["kind"] for e in api.get(f"{r}/events")[1]["events"]]
    assert "propose" in kinds and "revert" in kinds
    assert api.post(f"{r}/proposals/999/reject")[0] == 404
    status, data = api.post(f"{r}/jobs/test")
    assert status == 202 and api.wait_job(data["job"]["id"], timeout=180)["result"]["test_run"]["status"] == "failed"


def test_propose_in_manual_mode_reports_bundle(api, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "manual")
    repo = api.repo
    r = f"/api/repos/{repo.id}"
    api.post(f"{r}/init")
    api.wait_job(api.post(f"{r}/jobs/index")[1]["job"]["id"])
    job = api.wait_job(api.post(f"{r}/jobs/propose", {"task": "anything"})[1]["job"]["id"])
    assert job["status"] == "done" and job["result"]["manual"] is True and "prompt bundle" in job["result"]["message"]


def test_credentials_endpoint(api):
    status, data = api.get(f"/api/repos/{api.repo.id}/credentials")
    assert status == 200 and set(data) == {"identity", "ssh", "signing", "head"}
    assert data["ssh"]["kind"] in ("none", "ssh", "https", "local")


def test_terminal_create_list_delete(api):
    r = f"/api/repos/{api.repo.id}"
    assert api.post(f"{r}/terminals", {"cols": "x"})[0] == 400
    status, data = api.post(f"{r}/terminals", {"cols": 80, "rows": 24})
    assert status == 201 and data["terminal"]["alive"] is True
    tid = data["terminal"]["id"]
    assert [t["id"] for t in api.get(f"{r}/terminals")[1]["terminals"]] == [tid]
    assert api.request("DELETE", f"/api/terminals/{tid}")[0] == 200
    assert api.get(f"{r}/terminals")[1]["terminals"] == []
    assert api.request("DELETE", f"/api/terminals/{tid}")[0] == 404
```

- [ ] **Step 2: Write the failing WebSocket terminal test**

```python
# tests/test_ws_terminal.py
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
    client.read_until(b"$", timeout=3)  # any prompt output
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
```

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_ws_terminal.py -q -k "assistant or manual or credentials_endpoint or terminal or websocket"`
Expected: FAIL (404s from unknown routes, WebSocket status 404).

- [ ] **Step 4: Append assistant, settings, credentials and terminal routes**

```python
# argonauta/web/api.py (append)
# ----- assistant --------------------------------------------------------------------------
from argonauta.providers import ProviderUnavailable  # noqa: E402
from argonauta.runner import RunnerError, parse_command  # noqa: E402
from argonauta.web import credentials as credentials_mod  # noqa: E402
from argonauta.web.terminal import TerminalError  # noqa: E402
from argonauta import workflow  # noqa: E402


def _wf(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except workflow.ValidationError as exc:
        raise ApiError(400, str(exc)) from exc
    except (workflow.WorkflowError, StoreError) as exc:
        raise ApiError(409, str(exc)) from exc
    except gitops.GitError as exc:
        raise ApiError(409, str(exc)) from exc


def _proposal_payload(store: Store, pid: int) -> dict:
    proposal = store.get_proposal(pid)
    change = store.change_for_proposal(pid)
    runs = store.list_test_runs(pid, limit=10)
    return {"proposal": jsonable(proposal), "change": jsonable(change), "test_runs": jsonable(runs)}


@route("POST", rf"^/api/repos/(?P<rid>{ID})/init$")
def init_repo(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    command = require_str(body, "test_command", 1000, allow_empty=True).strip() or None
    settings = repo_settings(repo)
    store = _wf(lambda: workflow.init_repo(settings, command))
    store.close()
    return 201, repo.to_dict()


@route("GET", rf"^/api/repos/(?P<rid>{ID})/settings$")
def get_settings(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    settings = repo_settings(repo)
    store = open_store(repo)
    try:
        return 200, {"test_command": workflow.test_command_for(settings, store), "override_active": settings.test_command_override is not None, "provider": describe_provider(settings), "model": settings.model, "effort": settings.effort}
    finally:
        store.close()


@route("PUT", rf"^/api/repos/(?P<rid>{ID})/settings$")
def put_settings(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    command = require_str(body, "test_command", 1000)
    try:
        parse_command(command)
    except RunnerError as exc:
        raise ApiError(400, str(exc)) from exc
    store = open_store(repo)
    try:
        store.set_setting(workflow.TEST_COMMAND_KEY, command)
        store.record_event("config", "test command updated from the web UI")
    finally:
        store.close()
    return 200, {"test_command": command}


@route("GET", rf"^/api/repos/(?P<rid>{ID})/proposals$")
def list_proposals(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    store = open_store(repo)
    try:
        items = []
        for p in store.list_proposals(limit=query_int(query, "limit", 50, 1, 500)):
            change = store.change_for_proposal(p.id)
            run = store.latest_test_run_for_proposal(p.id)
            d = jsonable(p)
            d["diff"] = None  # list view is light; details carry the diff
            d["change"] = jsonable(change)
            d["last_test"] = jsonable(run)
            items.append(d)
        return 200, {"proposals": items}
    finally:
        store.close()


@route("GET", rf"^/api/repos/(?P<rid>{ID})/proposals/(?P<pid>\d{{1,9}})$")
def get_proposal(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    store = open_store(repo)
    try:
        try:
            return 200, _proposal_payload(store, int(params["pid"]))
        except StoreError as exc:
            raise ApiError(404, str(exc)) from exc
    finally:
        store.close()


@route("POST", rf"^/api/repos/(?P<rid>{ID})/proposals/(?P<pid>\d{{1,9}})/(?P<action>reject|revert|commit)$")
def proposal_action(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    pid = int(params["pid"])
    settings = repo_settings(repo)
    store = open_store(repo)
    try:
        try:
            store.get_proposal(pid)
        except StoreError as exc:
            raise ApiError(404, str(exc)) from exc
        action = params["action"]
        if action == "reject":
            _wf(lambda: workflow.reject_proposal(store, pid, require_str(body, "reason", 500, allow_empty=True).strip()))
        elif action == "revert":
            _wf(lambda: workflow.revert_proposal(settings, store, pid))
        else:
            _wf(lambda: workflow.commit_change(settings, store, pid))
        return 200, _proposal_payload(store, pid)
    finally:
        store.close()


@route("GET", rf"^/api/repos/(?P<rid>{ID})/events$")
def list_events(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    store = open_store(repo)
    try:
        return 200, {"events": jsonable(store.list_events(limit=query_int(query, "limit", 100, 1, 1000)))}
    finally:
        store.close()


@route("POST", rf"^/api/repos/(?P<rid>{ID})/jobs/(?P<kind>index|propose|test|apply)$")
def assistant_job(ctx, params, query, body):
    from argonauta.indexer import index_repo

    repo = resolve_repo(ctx, params["rid"])
    kind = params["kind"]
    if not repo.initialized:
        raise ApiError(409, "repository is not initialised; run init first")
    settings = repo_settings(repo)
    db_path = repo.path / ".argonauta" / "argonauta.db"

    if kind == "propose":
        task = _wf(lambda: workflow.validate_task(require_str(body, "task", workflow.MAX_TASK_CHARS)))
        dry_run = require_bool(body, "dry_run", False)
    elif kind == "apply":
        pid = body.get("proposal_id")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ApiError(400, "proposal_id must be a positive integer")
        run_tests = require_bool(body, "test", True)
        do_commit = require_bool(body, "commit", True)
    elif kind == "test":
        pid_opt = body.get("proposal_id")
        if pid_opt is not None and (not isinstance(pid_opt, int) or isinstance(pid_opt, bool) or pid_opt <= 0):
            raise ApiError(400, "proposal_id must be a positive integer")

    def run(progress):
        store = Store.open(db_path)
        try:
            if kind == "index":
                progress("indexing")
                stats = index_repo(store, repo.path, lambda n, total: progress(f"indexing {n}/{total}"))
                store.record_event("index", f"indexed {stats.indexed}, unchanged {stats.unchanged}, removed {stats.removed}, skipped {stats.skipped}")
                return {"stats": jsonable(stats), "file_count": store.file_count()}
            if kind == "propose":
                try:
                    outcome = workflow.propose(settings, store, task, dry_run=dry_run, on_progress=progress)
                except ProviderUnavailable as exc:
                    return {"manual": True, "message": str(exc), "context": []}
                return {
                    "manual": False,
                    "dry_run": dry_run,
                    "context": outcome.context.paths,
                    "skipped": outcome.context.skipped,
                    "proposal": jsonable(outcome.proposal),
                    "validation": {"ok": outcome.validation.ok, "errors": outcome.validation.errors, "files": outcome.validation.paths} if outcome.validation else None,
                }
            if kind == "apply":
                result = workflow.apply_proposal(settings, store, pid, run_tests_after=run_tests, commit_after=do_commit, on_progress=progress)
                return {"proposal": jsonable(result.proposal), "change": jsonable(result.change), "test_run": jsonable(result.test_run), "commit_sha": result.commit_sha, "skipped_commit_reason": result.skipped_commit_reason}
            progress("running tests")
            run_result = workflow.run_tests_now(settings, store, pid_opt)
            return {"test_run": jsonable(run_result)}
        finally:
            store.close()

    return submit_job(ctx, repo, kind, run)


# ----- credentials --------------------------------------------------------------------------
@route("GET", rf"^/api/repos/(?P<rid>{ID})/credentials$")
def get_credentials(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    return 200, credentials_mod.inspect(repo.path)


# ----- terminals ----------------------------------------------------------------------------
def _size(body: dict, key: str, default: int) -> int:
    value = body.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiError(400, f"{key} must be a positive integer")
    return value


@route("POST", rf"^/api/repos/(?P<rid>{ID})/terminals$")
def create_terminal(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    cols, rows = _size(body, "cols", 80), _size(body, "rows", 24)
    try:
        session = ctx.terminals.create(repo.path, cols, rows)
    except TerminalError as exc:
        raise ApiError(409, str(exc)) from exc
    record_event(repo, "terminal.open", f"terminal {session.id} opened")
    return 201, {"terminal": session.to_dict()}


@route("GET", rf"^/api/repos/(?P<rid>{ID})/terminals$")
def list_terminals(ctx, params, query, body):
    repo = resolve_repo(ctx, params["rid"])
    return 200, {"terminals": [t.to_dict() for t in ctx.terminals.list_for(repo.path)]}


@route("DELETE", rf"^/api/terminals/(?P<tid>{ID})$")
def delete_terminal(ctx, params, query, body):
    session = ctx.terminals.get(params["tid"])
    if session is None:
        raise ApiError(404, "terminal not found")
    ctx.terminals.remove(session.id)
    return 200, {"ok": True}
```

- [ ] **Step 5: Replace `_handle_websocket` in `server.py`**

```python
    # argonauta/web/server.py (replace the stub)
    def _handle_websocket(self, path: str) -> None:
        import re
        import threading

        from argonauta.web import websocket as wsproto

        m = re.match(r"^/ws/terminals/([A-Za-z0-9_]{3,40})$", path)
        if not self._origin_ok():
            self._send_json(403, {"error": "forbidden origin"})
            return
        if m is None or self.headers.get("Upgrade", "").lower() != "websocket" or not self.headers.get("Sec-WebSocket-Key"):
            self._send_json(404, {"error": "not found"})
            return
        session = self.ctx.terminals.get(m.group(1))
        if session is None:
            self._send_json(404, {"error": "terminal not found"})
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsproto.accept_key(self.headers["Sec-WebSocket-Key"]))
        self.end_headers()
        self.close_connection = True
        conn = wsproto.WebSocketConnection(self.rfile, self.wfile)
        try:
            first = conn.recv()
            try:
                auth = json.loads(first.payload.decode("utf-8")) if first.opcode == wsproto.OP_TEXT else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                auth = {}
            if auth.get("type") != "auth" or not isinstance(auth.get("token"), str) or not self._token_ok(auth["token"]):
                conn.close(1008)
                return
            session.resize(int(auth.get("cols") or session.cols), int(auth.get("rows") or session.rows))
        except (wsproto.ConnectionClosed, wsproto.ProtocolError, ValueError):
            conn.close(1002)
            return

        stop = threading.Event()

        def pump() -> None:
            try:
                while not stop.is_set():
                    data = session.read(0.1)
                    if data is None:
                        continue
                    if data == b"":
                        break
                    conn.send_binary(data)
            except wsproto.ConnectionClosed:
                pass
            finally:
                stop.set()
                try:
                    conn.close(1000)
                except Exception:  # noqa: BLE001
                    pass

        reader = threading.Thread(target=pump, name=f"pty-{session.id}", daemon=True)
        reader.start()
        try:
            while not stop.is_set():
                frame = conn.recv()
                if frame.opcode == wsproto.OP_BINARY:
                    session.write(frame.payload)
                elif frame.opcode == wsproto.OP_TEXT:
                    try:
                        msg = json.loads(frame.payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if msg.get("type") == "resize":
                        session.resize(int(msg.get("cols") or session.cols), int(msg.get("rows") or session.rows))
        except (wsproto.ConnectionClosed, wsproto.ProtocolError, ValueError, OSError):
            pass
        finally:
            stop.set()
            self.ctx.terminals.remove(session.id)
            reader.join(1)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_api.py tests/test_ws_terminal.py -q`
Expected: all PASS. If `test_terminal_shell_dies_when_socket_closes` is flaky because the prompt never contains `$`, change the probe to `client.send_binary(b"echo ready\n"); client.read_until(b"ready")`.

- [ ] **Step 7: Run the whole suite and commit**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

```bash
git add argonauta/web/api.py argonauta/web/server.py tests/test_api.py tests/test_ws_terminal.py
git commit -m "Add assistant, settings, credentials and terminal endpoints with WebSocket bridge"
```

---

### Task 10: `argonauta ui` command and server smoke test

**Files:**
- Create: `argonauta/web/app.py` (process lifecycle: build context, serve, open browser, shutdown)
- Modify: `argonauta/cli.py` (add `ui` command)
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: Task 7 `AppContext`, `create_server`, `new_token`; Task 2 `open_registry`, `RegistryError`; Task 3 `JobRunner`; Task 5 `TerminalManager`.
- Produces: `app.run_ui(host: str, port: int, open_browser: bool, start_repo: Path | None, announce: Callable[[str], None]) -> int` (blocks until SIGINT/SIGTERM; returns exit code). Prints exactly one line `Argonauta UI: http://127.0.0.1:<port>/#token=<token>` through `announce` before serving.

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_ui_smoke.py
import http.client
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import GIT_ENV


def test_ui_server_starts_serves_and_stops(tmp_path, sample_repo):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update(GIT_ENV)
    env["ARGONAUTA_CONFIG_DIR"] = str(tmp_path / "cfg")
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "argonauta", "--plain", "--repo", str(sample_repo), "ui", "--no-open", "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        line = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "Argonauta UI:" in line:
                break
        m = re.search(r"http://127\.0\.0\.1:(\d+)/#token=([A-Za-z0-9_\-]+)", line)
        assert m, f"no URL announced: {line!r}"
        port, token = int(m.group(1)), m.group(2)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200 and b"Argonauta" in resp.read()
        conn.request("GET", "/api/workspaces", headers={"Authorization": f"Bearer {token}", "Origin": f"http://127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200 and sample_repo.name in body  # auto-registered in Default
        conn.close()
        assert (tmp_path / "cfg" / "workspaces.json").is_file()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("ui did not stop on SIGINT")
    assert proc.returncode == 0


def test_ui_refuses_non_loopback_host(tmp_path, sample_repo):
    env = {**os.environ, **GIT_ENV, "ARGONAUTA_CONFIG_DIR": str(tmp_path / "cfg")}
    proc = subprocess.run([sys.executable, "-m", "argonauta", "--plain", "--repo", str(sample_repo), "ui", "--no-open", "--host", "0.0.0.0"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 2 and "127.0.0.1" in proc.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_smoke.py -q`
Expected: FAIL (`ui` is not a command; exit code 2 with usage error, no URL line).

- [ ] **Step 3: Implement `app.py`**

```python
# argonauta/web/app.py
"""Lifecycle of `argonauta ui`: context, server thread, browser, clean stop."""

from __future__ import annotations

import signal
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from argonauta.logging_setup import get_logger
from argonauta.web.jobs import JobRunner
from argonauta.web.server import AppContext, create_server, new_token
from argonauta.web.terminal import TerminalManager
from argonauta.workspaces import RegistryError, open_registry

log = get_logger()
STATIC_DIR = Path(__file__).resolve().parent / "static"


def run_ui(host: str, port: int, open_browser: bool, start_repo: Path | None, announce: Callable[[str], None]) -> int:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the UI only listens on 127.0.0.1 or localhost")
    try:
        registry = open_registry()
        if start_repo is not None:
            registry.ensure_repo(start_repo)
    except RegistryError as exc:
        raise ValueError(str(exc)) from exc
    ctx = AppContext(registry=registry, jobs=JobRunner(), terminals=TerminalManager(), token=new_token(), static_dir=STATIC_DIR)
    server = create_server(ctx, host=host, port=port)
    url = f"http://{host}:{ctx.port}/#token={ctx.token}"
    announce(f"Argonauta UI: {url}")
    stop = threading.Event()

    def on_signal(signum, frame):  # noqa: ARG001
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="argonauta-ui", daemon=True)
    thread.start()
    log.info("ui listening on port %s", ctx.port)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        server.shutdown()
        server.server_close()
        ctx.terminals.shutdown()
        ctx.jobs.shutdown()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        log.info("ui stopped")
    return 0
```

- [ ] **Step 4: Add the `ui` command to `cli.py`**

The console-helper module is imported as `ui` in `cli.py`, which clashes with a command named `ui`. First rename the import: change the top line to `from argonauta import __version__, gitops, ui as console_ui` and run `sed -i '' 's/\bui\./console_ui./g' argonauta/cli.py`, then run the full suite to prove nothing changed. Then add:

```python
@app.command()
def ui(
    port: Annotated[int, typer.Option("--port", "-p", min=0, max=65535, help="Port to listen on (0 = pick a free port).")] = 0,
    host: Annotated[str, typer.Option("--host", help="127.0.0.1 or localhost only.")] = "127.0.0.1",
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the page in your browser.")] = True,
) -> None:
    """Start the local web UI (commit graph, staging, branches, terminal, assistant)."""
    from argonauta.web.app import run_ui

    if host not in ("127.0.0.1", "localhost"):
        console_ui.failure("The UI only listens on 127.0.0.1 or localhost; it is a single-user tool.")
        raise typer.Exit(EXIT_INVALID)
    start_repo: Path | None = None
    try:
        start_repo = gitops.repo_toplevel((state.repo_override or Path.cwd()).resolve())
    except gitops.GitError:
        start_repo = None  # the page lets you add repositories
    console_ui.info("Press Ctrl+C to stop. Everything stays on this machine.")
    try:
        code = run_ui(host=host, port=port, open_browser=open_browser, start_repo=start_repo, announce=lambda line: (typer.echo(line), sys.stdout.flush()))
    except ValueError as exc:
        console_ui.failure(str(exc))
        raise typer.Exit(EXIT_INVALID)
    except OSError as exc:
        console_ui.failure(f"Could not start the server: {exc.strerror or exc}")
        raise typer.Exit(EXIT_FAIL)
    raise typer.Exit(code)
```

- [ ] **Step 5: Run the smoke test and the suite**

Run: `.venv/bin/python -m pytest tests/test_ui_smoke.py -q && .venv/bin/python -m pytest -q`
Expected: PASS. If the URL line never appears, check that `announce` prints before `serve_forever` starts and that `PYTHONUNBUFFERED` reaches the child.

- [ ] **Step 6: Commit**

```bash
git add argonauta/web/app.py argonauta/cli.py tests/test_ui_smoke.py
git commit -m "Add argonauta ui command with loopback server lifecycle"
```

---

### Task 11: Front end (page, styles, app logic, vendored xterm.js)

**Files:**
- Create: `argonauta/web/static/vendor/xterm.js`, `vendor/xterm.css`, `vendor/addon-fit.js`, `vendor/VERSION`, `vendor/LICENSE`
- Replace: `argonauta/web/static/index.html`
- Create: `argonauta/web/static/app.css`, `argonauta/web/static/app.js`
- Modify: `pyproject.toml` (package data), `tests/test_api.py` (`test_index_and_static_served_without_token` now expects 200 for `/app.js`)

**Interfaces:**
- Consumes every endpoint from Tasks 7 to 9 exactly as named there. Token arrives in `location.hash` as `#token=...`.
- Produces: the page. No JavaScript module system; `app.js` is one IIFE. Globals from vendor: `Terminal` (class) and `FitAddon.FitAddon`.

- [ ] **Step 1: Vendor xterm.js**

```bash
cd /tmp && rm -rf xt && mkdir xt && cd xt
curl -sSL https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz | tar xz
curl -sSL https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-0.10.0.tgz -o fit.tgz && mkdir fit && tar xzf fit.tgz -C fit
V=/Users/jonathandemarchi/workspace/personal/argonauta/argonauta/web/static/vendor
mkdir -p "$V"
cp package/lib/xterm.js "$V/xterm.js"
cp package/css/xterm.css "$V/xterm.css"
cp package/LICENSE "$V/LICENSE"
cp fit/package/lib/addon-fit.js "$V/addon-fit.js"
printf '@xterm/xterm 5.5.0\n@xterm/addon-fit 0.10.0\nSource: registry.npmjs.org tarballs, unmodified lib/ builds. License: MIT (see LICENSE).\n' > "$V/VERSION"
ls -la "$V"
```

Expected: five files; `xterm.js` around 300 KB. Verify the globals: `grep -c "Terminal" "$V/xterm.js"` is non-zero and `head -c 200 "$V/addon-fit.js"` mentions `FitAddon`. If the UMD global names differ, open the first line of each file: the `define`/`exports` name is the global to use in `app.js`.

Add to `pyproject.toml`:

```toml
[tool.setuptools.package-data]
argonauta = ["web/static/*.html", "web/static/*.js", "web/static/*.css", "web/static/vendor/*"]
```

- [ ] **Step 2: Write `index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Argonauta</title>
<link rel="stylesheet" href="/vendor/xterm.css">
<link rel="stylesheet" href="/app.css">
</head>
<body>
<a class="skip" href="#graph-panel">Skip to commit graph</a>
<header class="topbar" role="banner">
  <div class="brand">Argonauta</div>
  <label class="field"><span>Workspace</span><select id="workspace-select" aria-label="Workspace"></select></label>
  <label class="field"><span>Repository</span><select id="repo-select" aria-label="Repository"></select></label>
  <div class="branch-pill" id="branch-pill" aria-live="polite" title="Current branch"></div>
  <div class="spacer"></div>
  <div class="actions" role="group" aria-label="Remote actions">
    <button id="btn-fetch" class="btn" type="button">Fetch</button>
    <button id="btn-pull" class="btn" type="button">Pull</button>
    <button id="btn-push" class="btn" type="button">Push</button>
  </div>
  <div class="actions" role="group" aria-label="Panels">
    <button id="btn-terminal" class="btn" type="button">Terminal</button>
    <button id="btn-credentials" class="btn" type="button">Credentials</button>
    <button id="btn-help" class="btn ghost" type="button" aria-label="Keyboard shortcuts">?</button>
  </div>
</header>

<main class="layout">
  <aside class="left" aria-label="Repository sidebar">
    <section class="box" aria-labelledby="h-workspaces">
      <div class="box-head"><h2 id="h-workspaces">Workspaces</h2>
        <button class="btn mini" id="btn-add-workspace" type="button">+ Workspace</button>
        <button class="btn mini" id="btn-add-repo" type="button">+ Repo</button></div>
      <ul id="workspace-list" class="tree" role="tree" aria-label="Workspaces and repositories"></ul>
    </section>
    <section class="box" aria-labelledby="h-branches">
      <div class="box-head"><h2 id="h-branches">Branches</h2><button class="btn mini" id="btn-new-branch" type="button">+ Branch</button></div>
      <ul id="branch-list" class="list" role="listbox" aria-label="Local branches"></ul>
      <h3>Remotes</h3>
      <ul id="remote-branch-list" class="list" role="listbox" aria-label="Remote branches"></ul>
    </section>
    <section class="box grow" aria-labelledby="h-changes">
      <div class="box-head"><h2 id="h-changes">Changes</h2><span id="changes-count" class="muted"></span></div>
      <div id="changes-empty" class="empty" hidden>Working tree is clean.</div>
      <div id="conflicts" class="warn" hidden></div>
      <h3>Staged <button class="btn mini" id="btn-unstage-all" type="button">Unstage all</button></h3>
      <ul id="staged-list" class="files" aria-label="Staged files"></ul>
      <h3>Unstaged <button class="btn mini" id="btn-stage-all" type="button">Stage all</button></h3>
      <ul id="unstaged-list" class="files" aria-label="Unstaged and untracked files"></ul>
      <form id="commit-form" class="commit-form">
        <label for="commit-message">Commit message</label>
        <textarea id="commit-message" rows="3" maxlength="5000" placeholder="Summary of the change" required></textarea>
        <div class="row">
          <label class="check"><input type="checkbox" id="commit-amend"> Amend</label>
          <button class="btn primary" id="btn-commit" type="submit">Commit</button>
        </div>
        <div id="commit-error" class="field-error" role="alert"></div>
      </form>
    </section>
  </aside>

  <section class="center" id="graph-panel" aria-label="Commit graph" tabindex="-1">
    <div class="box-head sticky">
      <h2>Commits</h2>
      <input id="commit-filter" type="search" placeholder="Filter commits (/)" aria-label="Filter commits">
      <span id="graph-count" class="muted"></span>
    </div>
    <div id="graph-empty" class="empty" hidden>No commits yet. Stage files and commit to start the graph.</div>
    <div id="graph" class="graph" role="listbox" aria-label="Commits"></div>
  </section>

  <section class="right" aria-label="Details">
    <div class="tabs" role="tablist" aria-label="Detail panels">
      <button class="tab" role="tab" data-tab="detail" aria-selected="true" id="tab-detail">Detail</button>
      <button class="tab" role="tab" data-tab="credentials" aria-selected="false" id="tab-credentials">Credentials</button>
    </div>
    <div id="pane-detail" role="tabpanel" aria-labelledby="tab-detail" class="pane">
      <div class="empty">Select a file, a commit or a proposal to see its diff.</div>
    </div>
    <div id="pane-credentials" role="tabpanel" aria-labelledby="tab-credentials" class="pane" hidden></div>
  </section>
</main>

<section class="bottom" aria-label="Assistant and terminals">
  <div class="tabs" role="tablist" aria-label="Bottom panels" id="bottom-tabs">
    <button class="tab" role="tab" data-tab="proposals" aria-selected="true">Proposals</button>
    <button class="tab" role="tab" data-tab="events" aria-selected="false">Events</button>
  </div>
  <div id="pane-proposals" role="tabpanel" class="pane">
    <div id="store-missing" class="empty" hidden>
      This repository has no Argonauta store yet.
      <button class="btn primary" id="btn-init" type="button">Initialize</button>
    </div>
    <div id="assistant" hidden>
      <form id="propose-form" class="propose-form">
        <label for="task">Ask for a change</label>
        <div class="row">
          <input id="task" type="text" maxlength="4000" placeholder="e.g. Make power() handle negative exponents" required>
          <label class="check"><input type="checkbox" id="dry-run"> Dry run</label>
          <button class="btn primary" id="btn-propose" type="submit">Propose</button>
          <button class="btn" id="btn-index" type="button">Index</button>
          <button class="btn" id="btn-test" type="button">Run tests</button>
        </div>
        <div class="muted" id="provider-line"></div>
        <div id="propose-error" class="field-error" role="alert"></div>
      </form>
      <div id="proposals-empty" class="empty" hidden>No proposals yet. Describe a change above.</div>
      <table id="proposals-table" class="table" hidden>
        <thead><tr><th scope="col">#</th><th scope="col">Status</th><th scope="col">Tests</th><th scope="col">Commit</th><th scope="col">Task</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>
  <div id="pane-events" role="tabpanel" class="pane" hidden><table class="table" id="events-table"><thead><tr><th scope="col">Time</th><th scope="col">Kind</th><th scope="col">Message</th></tr></thead><tbody></tbody></table></div>
  <div id="terminal-panes"></div>
</section>

<div id="toasts" class="toasts" aria-live="polite" aria-atomic="false"></div>
<dialog id="dialog" class="dialog">
  <form method="dialog" id="dialog-form">
    <h2 id="dialog-title"></h2>
    <p id="dialog-text"></p>
    <label id="dialog-input-label" hidden><span id="dialog-input-name"></span><input id="dialog-input" type="text" maxlength="4000"></label>
    <div class="row end">
      <button class="btn" value="cancel" type="submit">Cancel</button>
      <button class="btn primary" value="ok" id="dialog-ok" type="submit">OK</button>
    </div>
  </form>
</dialog>
<dialog id="help" class="dialog">
  <form method="dialog">
    <h2>Keyboard shortcuts</h2>
    <dl class="shortcuts">
      <dt>p</dt><dd>Focus the proposal task field</dd>
      <dt>c</dt><dd>Focus the commit message</dd>
      <dt>t</dt><dd>Open a new terminal tab</dd>
      <dt>/</dt><dd>Filter commits</dd>
      <dt>Esc</dt><dd>Close dialogs</dd>
      <dt>?</dt><dd>This help</dd>
    </dl>
    <div class="row end"><button class="btn primary" value="close" type="submit">Close</button></div>
  </form>
</dialog>

<script src="/vendor/xterm.js"></script>
<script src="/vendor/addon-fit.js"></script>
<script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Write `app.css`**

```css
:root {
  --bg: #f6f7f9; --panel: #ffffff; --line: #d7dbe0; --text: #1b1f24; --muted: #5c6670;
  --accent: #1f5fbf; --accent-text: #ffffff; --ok: #1a7f37; --warn: #9a6700; --bad: #b42318;
  --add-bg: #e6f4ea; --add-text: #0f5b2a; --del-bg: #fdecea; --del-text: #8a1c14; --hunk: #eef1f5;
  --focus: #ff8c00; --lane0: #1f5fbf; --lane1: #c2410c; --lane2: #15803d; --lane3: #7e22ce; --lane4: #0e7490; --lane5: #b91c1c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216; --panel: #171b21; --line: #2b323b; --text: #e6e8eb; --muted: #9aa4b0;
    --accent: #6ea8fe; --accent-text: #0b1220; --ok: #4ade80; --warn: #fbbf24; --bad: #f87171;
    --add-bg: #10301c; --add-text: #9be3b4; --del-bg: #3a1614; --del-text: #f5b2ad; --hunk: #1f2630;
    --focus: #ffb347; --lane0: #6ea8fe; --lane1: #fb923c; --lane2: #4ade80; --lane3: #c084fc; --lane4: #22d3ee; --lane5: #f87171;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); display: grid; grid-template-rows: auto 1fr minmax(180px, 32vh); height: 100vh; }
.skip { position: absolute; left: -999px; top: 0; background: var(--accent); color: var(--accent-text); padding: 8px; z-index: 10; }
.skip:focus { left: 8px; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
button, input, select, textarea { font: inherit; color: inherit; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; margin: 0; color: var(--muted); }
h3 { font-size: 12px; margin: 10px 0 4px; color: var(--muted); display: flex; align-items: center; gap: 8px; }
.muted { color: var(--muted); }
.topbar { display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--panel); border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.brand { font-weight: 700; letter-spacing: .02em; }
.field { display: flex; flex-direction: column; font-size: 11px; color: var(--muted); }
.field select { min-width: 160px; padding: 4px 6px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
.branch-pill { padding: 4px 10px; border: 1px solid var(--line); border-radius: 999px; background: var(--bg); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.spacer { flex: 1; }
.actions { display: flex; gap: 6px; }
.btn { padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); cursor: pointer; }
.btn:hover { border-color: var(--accent); }
.btn:disabled { opacity: .55; cursor: progress; }
.btn.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
.btn.danger { color: var(--bad); border-color: var(--bad); }
.btn.mini { padding: 2px 6px; font-size: 11px; }
.btn.ghost { border-color: transparent; }
.layout { display: grid; grid-template-columns: minmax(240px, 300px) minmax(320px, 1fr) minmax(320px, 1.1fr); min-height: 0; }
.left, .center, .right { min-height: 0; overflow: auto; border-right: 1px solid var(--line); background: var(--panel); }
.right { border-right: 0; }
.box { padding: 10px 12px; border-bottom: 1px solid var(--line); }
.box.grow { border-bottom: 0; }
.box-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.box-head.sticky { position: sticky; top: 0; background: var(--panel); padding: 10px 12px; border-bottom: 1px solid var(--line); z-index: 1; }
.box-head h2 { flex: 1; }
.tree, .list, .files { list-style: none; margin: 0; padding: 0; }
.tree li, .list li { padding: 3px 6px; border-radius: 4px; cursor: pointer; display: flex; gap: 6px; align-items: center; }
.tree li.ws { font-weight: 600; cursor: default; margin-top: 4px; }
.tree li.repo { padding-left: 18px; }
.tree li.repo.missing { color: var(--bad); }
.list li[aria-selected="true"], .tree li.active { background: color-mix(in srgb, var(--accent) 16%, transparent); }
.list li .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.badge { font-size: 11px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
.files li { display: grid; grid-template-columns: auto auto 1fr auto; gap: 6px; align-items: center; padding: 2px 4px; border-radius: 4px; }
.files li:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.files .path { font-family: ui-monospace, Menlo, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; background: none; border: 0; text-align: left; padding: 0; }
.files .code { font-family: ui-monospace, Menlo, monospace; font-size: 11px; width: 1.4em; text-align: center; }
.code.M { color: var(--warn); } .code.A, .code.\? { color: var(--ok); } .code.D { color: var(--bad); }
.commit-form { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.commit-form textarea, .propose-form input[type=text], .dialog input { width: 100%; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.row.end { justify-content: flex-end; }
.check { display: flex; gap: 4px; align-items: center; font-size: 12px; }
.field-error { color: var(--bad); font-size: 12px; min-height: 1em; }
.empty { padding: 16px; color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; margin: 8px 0; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.warn { padding: 8px; color: var(--warn); border: 1px solid var(--warn); border-radius: 6px; font-size: 12px; }
/* graph */
.graph { position: relative; font-size: 13px; }
.graph .row-c { display: grid; grid-template-columns: var(--lanes-width, 60px) 1fr auto auto; gap: 8px; align-items: center; height: 28px; padding: 0 12px 0 0; cursor: pointer; border-radius: 4px; }
.graph .row-c:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.graph .row-c[aria-selected="true"] { background: color-mix(in srgb, var(--accent) 18%, transparent); }
.graph svg { position: absolute; left: 0; top: 0; pointer-events: none; overflow: visible; }
.graph .subject { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.graph .ref { font-size: 11px; padding: 0 6px; border-radius: 4px; border: 1px solid var(--accent); color: var(--accent); margin-right: 4px; }
.graph .ref.head { background: var(--accent); color: var(--accent-text); }
.graph .sha { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--muted); }
.sig { font-size: 11px; width: 1.6em; text-align: center; }
.sig.good { color: var(--ok); } .sig.bad, .sig.expired_signature, .sig.revoked { color: var(--bad); } .sig.unknown_key, .sig.expired_key, .sig.good_untrusted { color: var(--warn); } .sig.unsigned { color: var(--muted); }
/* right pane */
.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); padding: 4px 8px 0; background: var(--panel); position: sticky; top: 0; z-index: 1; }
.tab { padding: 6px 10px; border: 1px solid transparent; border-bottom: 0; background: none; cursor: pointer; border-radius: 6px 6px 0 0; }
.tab[aria-selected="true"] { border-color: var(--line); background: var(--bg); font-weight: 600; }
.tab .close { margin-left: 6px; color: var(--muted); }
.pane { padding: 10px 12px; min-height: 0; }
.diff { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; white-space: pre; overflow: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
.diff div { padding: 0 8px; }
.diff .add { background: var(--add-bg); color: var(--add-text); }
.diff .del { background: var(--del-bg); color: var(--del-text); }
.diff .hunk { background: var(--hunk); color: var(--muted); }
.diff .meta { color: var(--muted); font-weight: 600; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 8px 0; font-size: 13px; }
.kv dt { color: var(--muted); } .kv dd { margin: 0; word-break: break-all; }
.status-ok { color: var(--ok); } .status-warn { color: var(--warn); } .status-bad { color: var(--bad); }
.table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table th, .table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); }
.table tbody tr { cursor: pointer; }
.table tbody tr:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
/* bottom */
.bottom { display: grid; grid-template-rows: auto 1fr; min-height: 0; border-top: 1px solid var(--line); background: var(--panel); }
.bottom .pane { overflow: auto; }
.propose-form { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.propose-form input[type=text] { flex: 1; min-width: 240px; }
.term-pane { height: 100%; padding: 4px; background: #000; }
.term-pane .xterm { height: 100%; }
/* toasts and dialogs */
.toasts { position: fixed; right: 12px; bottom: 12px; display: flex; flex-direction: column; gap: 8px; z-index: 20; max-width: 420px; }
.toast { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); box-shadow: 0 6px 24px rgba(0,0,0,.18); user-select: text; }
.toast.ok { border-color: var(--ok); } .toast.bad { border-color: var(--bad); } .toast.warn { border-color: var(--warn); }
.toast .sym { font-weight: 700; margin-right: 6px; }
.dialog { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text); padding: 16px 18px; min-width: 360px; max-width: 90vw; }
.dialog::backdrop { background: rgba(0,0,0,.45); }
.shortcuts { display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; }
.shortcuts dt { font-family: ui-monospace, Menlo, monospace; }
.spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -2px; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 1023px) {
  .layout { grid-template-columns: 1fr; grid-auto-rows: minmax(240px, auto); overflow: auto; }
  .left, .center, .right { border-right: 0; border-bottom: 1px solid var(--line); }
}
```

- [ ] **Step 4: Write `app.js`**

```javascript
/* Argonauta web UI. One file, no build step, no network beyond the local server. */
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LANES = ["--lane0", "--lane1", "--lane2", "--lane3", "--lane4", "--lane5"];
  const SIG = { good: "✓", bad: "✗", good_untrusted: "~", expired_signature: "⌛", expired_key: "⌛", revoked: "✗", unknown_key: "?", unsigned: "·" };

  const S = {
    token: null, workspaces: [], repoId: null, state: null, graph: [], proposals: [], events: [],
    selection: null, rightTab: "detail", bottomTab: "proposals", terminals: new Map(), busy: new Set(), filter: "", pollTimer: null,
  };

  // ----- token and api -----------------------------------------------------------------
  function readToken() {
    const m = /[#&]token=([A-Za-z0-9_\-]+)/.exec(location.hash);
    if (m) { try { sessionStorage.setItem("argonauta.token", m[1]); } catch (e) { /* private mode */ } history.replaceState(null, "", location.pathname); return m[1]; }
    try { return sessionStorage.getItem("argonauta.token"); } catch (e) { return null; }
  }
  async function api(method, path, body) {
    const headers = { Authorization: "Bearer " + S.token };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) { const err = new Error((data && data.error) || ("HTTP " + resp.status)); err.status = resp.status; err.data = data; throw err; }
    return data;
  }
  const rp = (suffix) => "/api/repos/" + S.repoId + suffix;

  // ----- toasts, dialogs, busy ---------------------------------------------------------------
  function toast(kind, text, sticky) {
    const el = document.createElement("div");
    el.className = "toast " + kind; el.setAttribute("role", kind === "bad" ? "alert" : "status");
    el.innerHTML = '<span class="sym">' + ({ ok: "✓", bad: "✗", warn: "!", info: "·" }[kind] || "·") + "</span>" + esc(text);
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), sticky ? 15000 : 5000);
  }
  function dialog(title, text, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const d = $("#dialog");
      $("#dialog-title").textContent = title; $("#dialog-text").textContent = text;
      const label = $("#dialog-input-label"), input = $("#dialog-input");
      label.hidden = !opts.input; $("#dialog-input-name").textContent = opts.input || ""; input.value = opts.value || "";
      $("#dialog-ok").textContent = opts.ok || "OK"; $("#dialog-ok").className = "btn " + (opts.danger ? "danger" : "primary");
      const onClose = () => { d.removeEventListener("close", onClose); resolve(d.returnValue === "ok" ? (opts.input ? input.value : true) : null); };
      d.addEventListener("close", onClose); d.showModal(); if (opts.input) input.focus();
    });
  }
  function setBusy(key, on) {
    if (on) S.busy.add(key); else S.busy.delete(key);
    const anyRemote = ["fetch", "pull", "push"].some((k) => S.busy.has(k));
    ["fetch", "pull", "push"].forEach((k) => { const b = $("#btn-" + k); b.disabled = anyRemote; b.innerHTML = S.busy.has(k) ? '<span class="spinner"></span>' + k[0].toUpperCase() + k.slice(1) : k[0].toUpperCase() + k.slice(1); });
    const assistantBusy = ["index", "propose", "test", "apply"].some((k) => S.busy.has(k));
    ["propose", "index", "test"].forEach((k) => { const b = $("#btn-" + k); if (b) b.disabled = assistantBusy; });
    $("#btn-commit").disabled = S.busy.has("commit");
  }

  // ----- jobs ------------------------------------------------------------------------------
  async function runJob(kind, body, label) {
    setBusy(kind, true);
    const status = toastProgress(label || kind);
    try {
      const data = await api("POST", rp("/jobs/" + kind), body || {});
      let job = data.job;
      while (job.status === "queued" || job.status === "running") {
        await new Promise((r) => setTimeout(r, 500));
        job = await api("GET", "/api/jobs/" + job.id);
        status.update(job.progress || label || kind);
      }
      status.remove();
      if (job.status === "failed") { toast("bad", (label || kind) + ": " + job.error, true); return null; }
      return job.result;
    } catch (err) {
      status.remove();
      if (err.status === 409 && err.data && err.data.job) toast("warn", "Another operation is running: " + err.data.job.kind);
      else toast("bad", (label || kind) + ": " + err.message, true);
      return null;
    } finally { setBusy(kind, false); refreshState(); }
  }
  function toastProgress(text) {
    const el = document.createElement("div"); el.className = "toast info"; el.setAttribute("role", "status");
    el.innerHTML = '<span class="spinner"></span><span class="t"></span>'; $(".t", el).textContent = text; $("#toasts").appendChild(el);
    return { update: (t) => { $(".t", el).textContent = t; }, remove: () => el.remove() };
  }

  // ----- workspaces ---------------------------------------------------------------------------
  async function loadWorkspaces() {
    const data = await api("GET", "/api/workspaces");
    S.workspaces = data.workspaces;
    const repos = S.workspaces.flatMap((w) => w.repos.map((r) => ({ ...r, workspace: w })));
    let remembered = null; try { remembered = localStorage.getItem("argonauta.repo"); } catch (e) { /* ignore */ }
    if (!S.repoId || !repos.some((r) => r.id === S.repoId)) {
      const pick = repos.find((r) => r.id === remembered && r.exists) || repos.find((r) => r.exists) || null;
      S.repoId = pick ? pick.id : null;
    }
    renderWorkspaces();
  }
  function renderWorkspaces() {
    const wsSel = $("#workspace-select"), repoSel = $("#repo-select"), tree = $("#workspace-list");
    wsSel.innerHTML = S.workspaces.map((w) => '<option value="' + esc(w.id) + '">' + esc(w.name) + "</option>").join("");
    const current = S.workspaces.find((w) => w.repos.some((r) => r.id === S.repoId)) || S.workspaces[0];
    if (current) wsSel.value = current.id;
    repoSel.innerHTML = (current ? current.repos : []).map((r) => '<option value="' + esc(r.id) + '"' + (r.exists ? "" : " disabled") + ">" + esc(r.name) + (r.exists ? "" : " (missing)") + "</option>").join("");
    if (S.repoId) repoSel.value = S.repoId;
    tree.innerHTML = S.workspaces.map((w) => '<li class="ws" role="treeitem" aria-expanded="true">' + esc(w.name) + ' <button class="btn mini" data-del-ws="' + esc(w.id) + '" aria-label="Remove workspace ' + esc(w.name) + '">×</button></li>' +
      w.repos.map((r) => '<li class="repo' + (r.id === S.repoId ? " active" : "") + (r.exists ? "" : " missing") + '" role="treeitem" tabindex="0" data-repo="' + esc(r.id) + '" title="' + esc(r.path) + '">' + esc(r.name) + (r.exists ? "" : " (missing)") + ' <button class="btn mini" data-del-repo="' + esc(r.id) + '" data-ws="' + esc(w.id) + '" aria-label="Remove repository ' + esc(r.name) + '">×</button></li>').join("")).join("");
    if (!S.workspaces.length) tree.innerHTML = '<li class="empty">No workspaces. Click “+ Workspace”, then “+ Repo”.</li>';
  }
  async function selectRepo(id) {
    S.repoId = id; S.selection = null; S.graph = []; S.proposals = [];
    try { localStorage.setItem("argonauta.repo", id); } catch (e) { /* ignore */ }
    renderWorkspaces(); renderDetail();
    await refreshAll();
  }

  // ----- state and graph -------------------------------------------------------------------
  async function refreshState() {
    if (!S.repoId) { S.state = null; renderState(); return; }
    try { S.state = await api("GET", rp("/state")); } catch (err) { S.state = null; toast("bad", "State: " + err.message); }
    renderState();
  }
  async function refreshGraph() {
    if (!S.repoId) { S.graph = []; renderGraph(); return; }
    try { S.graph = (await api("GET", rp("/graph?limit=300"))).commits; } catch (err) { S.graph = []; }
    renderGraph();
  }
  async function refreshAssistant() {
    if (!S.state || !S.state.store.initialized) { S.proposals = []; S.events = []; renderAssistant(); return; }
    try {
      S.proposals = (await api("GET", rp("/proposals?limit=100"))).proposals;
      S.events = (await api("GET", rp("/events?limit=200"))).events;
    } catch (err) { S.proposals = []; S.events = []; }
    renderAssistant();
  }
  async function refreshAll() { await refreshState(); await Promise.all([refreshGraph(), refreshAssistant()]); }

  function renderState() {
    const st = S.state;
    $("#branch-pill").textContent = st ? (st.branch + (st.upstream ? " → " + st.upstream + " (" + st.ahead + "↑ " + st.behind + "↓)" : " (no upstream)")) : "no repository";
    const bl = $("#branch-list"), rl = $("#remote-branch-list");
    if (!st) { bl.innerHTML = ""; rl.innerHTML = ""; renderChanges(null); $("#store-missing").hidden = true; $("#assistant").hidden = true; return; }
    const locals = st.branches.filter((b) => !b.remote), remotes = st.branches.filter((b) => b.remote);
    bl.innerHTML = locals.map((b) => '<li role="option" tabindex="0" aria-selected="' + b.is_current + '" data-branch="' + esc(b.name) + '"><span class="name">' + (b.is_current ? "● " : "○ ") + esc(b.name) + "</span>" +
      (b.upstream ? '<span class="badge" title="ahead/behind upstream">' + b.ahead + "↑" + b.behind + "↓" + (b.gone ? " gone" : "") + "</span>" : "") +
      (b.is_current ? "" : '<button class="btn mini" data-del-branch="' + esc(b.name) + '" aria-label="Delete branch ' + esc(b.name) + '">×</button>') + "</li>").join("") || '<li class="muted">No local branches yet.</li>';
    rl.innerHTML = remotes.map((b) => '<li role="option" tabindex="0" aria-selected="false" data-remote-branch="' + esc(b.name) + '"><span class="name">' + esc(b.name) + '</span><span class="badge">' + esc(b.sha) + "</span></li>").join("") || '<li class="muted">No remote branches (' + (st.remotes.length ? st.remotes.map((r) => r.name).join(", ") : "no remotes") + ").</li>";
    renderChanges(st.status);
    const store = st.store;
    $("#store-missing").hidden = store.initialized; $("#assistant").hidden = !store.initialized;
    $("#provider-line").textContent = store.initialized ? "Provider: " + store.provider + " · " + (store.indexed_files ? store.indexed_files + " files indexed" : "nothing indexed yet, click Index") + " · tests: " + (store.test_command || "not set") : "";
    if (store.provider_name === "manual" && store.initialized) $("#provider-line").textContent += " · degraded mode: no API key, proposals produce a prompt bundle";
    const running = st.running_job; if (running && !S.busy.has(running.kind)) watchJob(running);
  }
  function watchJob(job) { setBusy(job.kind, true); (async () => { let j = job; while (j.status === "queued" || j.status === "running") { await new Promise((r) => setTimeout(r, 700)); try { j = await api("GET", "/api/jobs/" + j.id); } catch (e) { break; } } setBusy(job.kind, false); refreshAll(); })(); }

  function renderChanges(status) {
    const staged = $("#staged-list"), unstaged = $("#unstaged-list");
    if (!status) { staged.innerHTML = ""; unstaged.innerHTML = ""; $("#changes-count").textContent = ""; $("#changes-empty").hidden = true; return; }
    const total = status.staged.length + status.unstaged.length + status.untracked.length + status.conflicted.length;
    $("#changes-count").textContent = total ? total + " changed" : ""; $("#changes-empty").hidden = total > 0;
    const conflicts = $("#conflicts"); conflicts.hidden = !status.conflicted.length;
    conflicts.textContent = status.conflicted.length ? "Conflicts in: " + status.conflicted.map((e) => e.path).join(", ") + ". Resolve them with git, then stage." : "";
    const row = (e, area, code) => '<li><input type="checkbox" aria-label="' + (area === "index" ? "Unstage " : "Stage ") + esc(e.path) + '" data-toggle="' + area + '" data-path="' + esc(e.path) + '"' + (area === "index" ? " checked" : "") + '><span class="code ' + esc(code) + '">' + esc(code) + '</span><button type="button" class="path" data-diff="' + area + '" data-path="' + esc(e.path) + '">' + esc(e.path) + '</button><button type="button" class="btn mini" data-discard="' + esc(e.path) + '" aria-label="Discard changes in ' + esc(e.path) + '">discard</button></li>';
    staged.innerHTML = status.staged.map((e) => row(e, "index", e.index)).join("") || '<li class="muted">Nothing staged.</li>';
    unstaged.innerHTML = status.unstaged.map((e) => row(e, "worktree", e.worktree)).concat(status.untracked.map((e) => row(e, "worktree", "?"))).join("") || '<li class="muted">No unstaged changes.</li>';
  }

  function renderGraph() {
    const box = $("#graph"), rows = S.filter ? S.graph.filter((c) => (c.subject + " " + c.author + " " + c.sha + " " + c.refs.join(" ")).toLowerCase().includes(S.filter)) : S.graph;
    $("#graph-empty").hidden = S.graph.length > 0; $("#graph-count").textContent = S.graph.length ? rows.length + " of " + S.graph.length : "";
    const maxLane = rows.reduce((m, c) => Math.max(m, c.lane, ...(c.parent_lanes || []), c.active.length - 1), 0);
    const laneW = 14, rowH = 28, x = (l) => 10 + l * laneW, y = (i) => i * rowH + rowH / 2;
    box.style.setProperty("--lanes-width", (x(maxLane) + 14) + "px");
    const color = (l) => "var(" + LANES[l % LANES.length] + ")";
    const paths = [];
    rows.forEach((c, i) => {
      const next = rows[i + 1];
      (c.active || []).forEach((on, col) => {
        if (!on) return;
        const merging = next && next.merged_lanes.includes(col) && next.lane !== col;
        if (col === c.lane) paths.push('<path d="M' + x(col) + "," + y(i) + " L" + x(col) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
        else if (c.parent_lanes.includes(col)) paths.push('<path d="M' + x(c.lane) + "," + y(i) + " C" + x(c.lane) + "," + (y(i) + rowH / 2) + " " + x(col) + "," + (y(i) + rowH / 2) + " " + x(col) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
        else if (merging) paths.push('<path d="M' + x(col) + "," + y(i) + " C" + x(col) + "," + (y(i) + rowH / 2) + " " + x(next.lane) + "," + (y(i) + rowH / 2) + " " + x(next.lane) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
        else paths.push('<path d="M' + x(col) + "," + y(i) + " L" + x(col) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
      });
      paths.push('<circle cx="' + x(c.lane) + '" cy="' + y(i) + '" r="4.5" fill="' + color(c.lane) + '"/>');
    });
    const svg = '<svg width="' + (x(maxLane) + 14) + '" height="' + (rows.length * rowH) + '" fill="none" stroke-width="2" aria-hidden="true">' + paths.join("") + "</svg>";
    box.innerHTML = svg + rows.map((c, i) => {
      const refs = c.refs.map((r) => '<span class="ref' + (r.startsWith("HEAD") ? " head" : "") + '">' + esc(r.replace("HEAD -> ", "")) + "</span>").join("");
      const sig = SIG[sigWord(c.signature)] || "·";
      return '<div class="row-c" role="option" tabindex="0" aria-selected="' + (S.selection && S.selection.type === "commit" && S.selection.sha === c.sha) + '" data-sha="' + esc(c.sha) + '" style="grid-row:' + (i + 1) + '"><span></span><span class="subject">' + refs + esc(c.subject) + '</span><span class="sig ' + sigWord(c.signature) + '" title="signature: ' + sigWord(c.signature) + '">' + sig + '</span><span class="sha">' + esc(c.short) + "</span></div>";
    }).join("");
  }
  function sigWord(code) { return { G: "good", B: "bad", U: "good_untrusted", X: "expired_signature", Y: "expired_key", R: "revoked", E: "unknown_key", N: "unsigned" }[code] || "unsigned"; }

  // ----- right pane ----------------------------------------------------------------------
  function diffHtml(diff) {
    if (!diff || !diff.trim()) return '<div class="empty">No differences.</div>';
    return '<div class="diff">' + diff.split("\n").map((line) => {
      let cls = "";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) cls = "meta";
      else if (line.startsWith("@@")) cls = "hunk"; else if (line.startsWith("+")) cls = "add"; else if (line.startsWith("-")) cls = "del";
      return '<div class="' + cls + '">' + (esc(line) || " ") + "</div>";
    }).join("") + "</div>";
  }
  async function showFileDiff(area, path) {
    S.selection = { type: "file", area, path }; selectTab("right", "detail");
    const pane = $("#pane-detail"); pane.innerHTML = '<div class="muted"><span class="spinner"></span>Loading ' + esc(path) + "…</div>";
    try { const d = await api("GET", rp("/diff?path=" + encodeURIComponent(path) + "&area=" + area)); pane.innerHTML = "<h2>" + esc(path) + " · " + (area === "index" ? "staged" : "working tree") + "</h2>" + diffHtml(d.diff); }
    catch (err) { pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  async function showCommit(sha) {
    S.selection = { type: "commit", sha }; selectTab("right", "detail"); renderGraph();
    const pane = $("#pane-detail"); pane.innerHTML = '<div class="muted"><span class="spinner"></span>Loading commit…</div>';
    try {
      const d = await api("GET", rp("/commits/" + sha));
      const c = d.commit, sig = sigWord(c.signature);
      pane.innerHTML = "<h2>" + esc(c.subject) + "</h2><dl class='kv'><dt>sha</dt><dd>" + esc(c.sha) + "</dd><dt>author</dt><dd>" + esc(c.author) + " &lt;" + esc(c.email) + "&gt;</dd><dt>date</dt><dd>" + esc(c.date) + "</dd><dt>signature</dt><dd class='sig " + sig + "'>" + (SIG[sig] || "·") + " " + sig.replace("_", " ") + (d.signer ? " · " + esc(d.signer) : "") + (d.key_id ? " · key " + esc(d.key_id) : "") + "</dd>" + (c.refs.length ? "<dt>refs</dt><dd>" + esc(c.refs.join(", ")) + "</dd>" : "") + "</dl>" + (d.body ? "<p>" + esc(d.body).replace(/\n/g, "<br>") + "</p>" : "") +
        "<ul class='files'>" + d.files.map((f) => "<li><span></span><span class='code " + esc(f.status) + "'>" + esc(f.status) + "</span><span class='path'>" + esc(f.path) + "</span><span class='muted'>+" + f.insertions + " −" + f.deletions + "</span></li>").join("") + "</ul>" + diffHtml(d.diff);
    } catch (err) { pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  async function showProposal(pid) {
    S.selection = { type: "proposal", id: pid }; selectTab("right", "detail");
    const pane = $("#pane-detail"); pane.innerHTML = '<div class="muted"><span class="spinner"></span>Loading proposal…</div>';
    try {
      const d = await api("GET", rp("/proposals/" + pid)); const p = d.proposal, ch = d.change, runs = d.test_runs;
      const can = { apply: p.status === "proposed", reject: ["proposed", "invalid"].includes(p.status), revert: p.status === "applied" && ch && !ch.reverted_at, commit: p.status === "applied" && ch && !ch.commit_sha };
      pane.innerHTML = "<h2>Proposal #" + p.id + (p.is_sample ? " [SAMPLE]" : "") + "</h2><dl class='kv'><dt>status</dt><dd>" + esc(p.status) + "</dd><dt>task</dt><dd>" + esc(p.task) + "</dd><dt>provider</dt><dd>" + esc(p.provider + " / " + p.model) + "</dd><dt>context</dt><dd>" + esc(p.context_files.join(", ") || "(none)") + "</dd>" + (p.notes ? "<dt>notes</dt><dd>" + esc(p.notes) + "</dd>" : "") + (ch ? "<dt>change</dt><dd>" + ch.files.length + " files, +" + ch.insertions + " −" + ch.deletions + (ch.commit_sha ? ", commit " + esc(ch.commit_sha.slice(0, 10)) : ", not committed") + (ch.reverted_at ? ", reverted" : "") + "</dd>" : "") + "</dl>" +
        '<div class="row">' + (can.apply ? '<label class="check"><input type="checkbox" id="apply-test" checked> run tests</label><label class="check"><input type="checkbox" id="apply-commit" checked> commit on green</label><button class="btn primary" data-prop-apply="' + p.id + '">Apply</button>' : "") + (can.reject ? '<button class="btn" data-prop-reject="' + p.id + '">Reject</button>' : "") + (can.commit ? '<button class="btn" data-prop-commit="' + p.id + '">Commit</button>' : "") + (can.revert ? '<button class="btn danger" data-prop-revert="' + p.id + '">Revert</button>' : "") + "</div>" +
        (p.explanation ? "<p>" + esc(p.explanation).replace(/\n/g, "<br>") + "</p>" : "") + diffHtml(p.diff) +
        (runs.length ? "<h3>Test runs</h3><table class='table'><thead><tr><th>#</th><th>status</th><th>exit</th><th>finished</th></tr></thead><tbody>" + runs.map((r) => "<tr><td>" + r.id + "</td><td class='status-" + (r.status === "passed" ? "ok" : "bad") + "'>" + esc(r.status) + "</td><td>" + esc(r.exit_code) + "</td><td>" + esc(r.finished_at) + "</td></tr>").join("") + "</tbody></table>" + (runs[0].status !== "passed" && runs[0].output_tail ? "<pre class='diff'>" + esc(runs[0].output_tail) + "</pre>" : "") : "");
    } catch (err) { pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  async function showCredentials() {
    selectTab("right", "credentials"); const pane = $("#pane-credentials");
    if (!S.repoId) { pane.innerHTML = '<div class="empty">Select a repository first.</div>'; return; }
    pane.innerHTML = '<div class="muted"><span class="spinner"></span>Inspecting credentials…</div>';
    try {
      const d = await api("GET", rp("/credentials")); const id = d.identity, ssh = d.ssh, sg = d.signing, head = d.head;
      const yn = (v) => v === true ? "<span class='status-ok'>yes</span>" : v === false ? "<span class='status-bad'>no</span>" : "<span class='muted'>unknown</span>";
      pane.innerHTML = "<h2>Git identity</h2><dl class='kv'><dt>name</dt><dd>" + esc(id.name || "unset") + "</dd><dt>email</dt><dd>" + esc(id.email || "unset") + "</dd><dt>source</dt><dd>" + esc(id.source) + "</dd></dl>" +
        "<h2>SSH</h2><dl class='kv'><dt>remote</dt><dd>" + esc(ssh.remote_url || "none") + "</dd><dt>kind</dt><dd>" + esc(ssh.kind) + "</dd>" + (ssh.kind === "ssh" ? "<dt>host alias</dt><dd>" + esc(ssh.host_alias) + " → " + esc(ssh.resolved_hostname || "?") + " as " + esc(ssh.user || "?") + "</dd><dt>agent running</dt><dd>" + yn(ssh.agent.running) + "</dd><dt>key loaded</dt><dd>" + yn(ssh.agent.loaded) + "</dd>" : "") + (ssh.kind === "https" ? "<dt>credential helper</dt><dd>" + esc(ssh.credential_helper) + "</dd>" : "") + "</dl>" +
        (ssh.identity_files.length ? "<ul class='files'>" + ssh.identity_files.map((f) => "<li><span></span><span class='code'>" + (f.exists ? "✓" : "✗") + "</span><span class='path'>" + esc(f.path) + (f.type ? " · " + esc(f.type) + " " + f.bits : "") + (f.comment ? " · " + esc(f.comment) : "") + "</span><span class='muted'>" + (f.fingerprint ? esc(f.fingerprint) : "no public key file") + (f.in_agent === true ? " · in agent" : f.in_agent === false ? " · not in agent" : "") + "</span></li>").join("") + "</ul>" : "") +
        "<h2>Commit signing</h2><dl class='kv'><dt>enabled</dt><dd>" + yn(sg.enabled) + "</dd>" + (sg.enabled ? "<dt>format</dt><dd>" + esc(sg.format) + "</dd><dt>key</dt><dd>" + esc(sg.key_id || "unset") + (sg.fingerprint ? " · " + esc(sg.fingerprint) : "") + "</dd><dt>uid</dt><dd>" + esc(sg.uid || "?") + "</dd><dt>valid</dt><dd>" + esc((sg.created || "?") + " → " + (sg.expires || "no expiry")) + "</dd><dt>status</dt><dd class='status-" + (sg.status === "ok" ? "ok" : "bad") + "'>" + esc(sg.status) + "</dd>" : "") + "</dl>" +
        "<h2>HEAD signature</h2><dl class='kv'><dt>commit</dt><dd>" + esc(head.sha || "none") + "</dd><dt>status</dt><dd class='sig " + esc(head.signature) + "'>" + (SIG[head.signature] || "·") + " " + esc(head.signature.replace("_", " ")) + (head.signer ? " · " + esc(head.signer) : "") + "</dd></dl><p class='muted'>Private keys are never read. Only public key files, agent listings and git/gpg metadata.</p>";
    } catch (err) { pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  function renderDetail() { if (!S.selection) $("#pane-detail").innerHTML = '<div class="empty">Select a file, a commit or a proposal to see its diff.</div>'; }

  // ----- assistant -----------------------------------------------------------------------
  function renderAssistant() {
    const table = $("#proposals-table"), body = $("tbody", table);
    $("#proposals-empty").hidden = S.proposals.length > 0 || !(S.state && S.state.store.initialized); table.hidden = !S.proposals.length;
    body.innerHTML = S.proposals.map((p) => "<tr tabindex='0' data-prop='" + p.id + "'><td>" + p.id + "</td><td>" + esc(p.status) + "</td><td>" + (p.last_test ? esc(p.last_test.status) : "–") + "</td><td>" + (p.change && p.change.commit_sha ? esc(p.change.commit_sha.slice(0, 8)) : "–") + "</td><td>" + esc(p.task) + "</td></tr>").join("");
    $("tbody", $("#events-table")).innerHTML = S.events.map((e) => "<tr><td>" + esc(e.ts.slice(0, 19).replace("T", " ")) + "</td><td>" + esc(e.kind) + "</td><td>" + esc(e.message) + "</td></tr>").join("") || "<tr><td colspan='3' class='muted'>No events yet.</td></tr>";
  }

  // ----- terminals -----------------------------------------------------------------------
  async function openTerminal() {
    if (!S.repoId) { toast("warn", "Select a repository first."); return; }
    let data;
    try { data = await api("POST", rp("/terminals"), { cols: 120, rows: 30 }); } catch (err) { toast("bad", "Terminal: " + err.message); return; }
    const t = data.terminal, tabs = $("#bottom-tabs");
    const tab = document.createElement("button"); tab.className = "tab"; tab.setAttribute("role", "tab"); tab.dataset.tab = "term-" + t.id; tab.innerHTML = "Terminal <span class='close' aria-label='Close terminal'>×</span>"; tabs.appendChild(tab);
    const pane = document.createElement("div"); pane.id = "pane-term-" + t.id; pane.className = "pane term-pane"; pane.setAttribute("role", "tabpanel"); pane.hidden = true; $("#terminal-panes").appendChild(pane);
    const term = new Terminal({ cursorBlink: true, fontSize: 13, theme: { background: "#000000" } }); const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(pane);
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/terminals/" + t.id); ws.binaryType = "arraybuffer";
    const entry = { id: t.id, term, fit, ws, tab, pane }; S.terminals.set(t.id, entry);
    ws.onopen = () => { selectTab("bottom", "term-" + t.id); fit.fit(); ws.send(JSON.stringify({ type: "auth", token: S.token, cols: term.cols, rows: term.rows })); term.focus(); };
    ws.onmessage = (ev) => term.write(new Uint8Array(ev.data));
    ws.onclose = () => { term.write("\r\n\x1b[2m[terminal closed]\x1b[0m\r\n"); };
    term.onData((d) => { if (ws.readyState === 1) ws.send(new TextEncoder().encode(d)); });
    term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows })); });
    $(".close", tab).addEventListener("click", (ev) => { ev.stopPropagation(); closeTerminal(t.id); });
  }
  async function closeTerminal(id) {
    const e = S.terminals.get(id); if (!e) return;
    try { e.ws.close(); } catch (err) { /* ignore */ }
    try { await api("DELETE", "/api/terminals/" + id); } catch (err) { /* already gone */ }
    e.term.dispose(); e.tab.remove(); e.pane.remove(); S.terminals.delete(id); selectTab("bottom", "proposals");
  }
  window.addEventListener("resize", () => S.terminals.forEach((e) => { if (!e.pane.hidden) e.fit.fit(); }));

  // ----- tabs ------------------------------------------------------------------------------
  function selectTab(region, name) {
    if (region === "right") {
      S.rightTab = name;
      $$(".right .tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
      $("#pane-detail").hidden = name !== "detail"; $("#pane-credentials").hidden = name !== "credentials";
    } else {
      S.bottomTab = name;
      $$("#bottom-tabs .tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
      $("#pane-proposals").hidden = name !== "proposals"; $("#pane-events").hidden = name !== "events";
      S.terminals.forEach((e) => { e.pane.hidden = e.tab.dataset.tab !== name; if (!e.pane.hidden) { e.fit.fit(); e.term.focus(); } });
    }
  }

  // ----- event wiring -----------------------------------------------------------------------
  function wire() {
    $("#workspace-select").addEventListener("change", (ev) => { const w = S.workspaces.find((x) => x.id === ev.target.value); const r = w && w.repos.find((x) => x.exists); if (r) selectRepo(r.id); else { S.repoId = null; renderWorkspaces(); refreshAll(); } });
    $("#repo-select").addEventListener("change", (ev) => selectRepo(ev.target.value));
    $("#workspace-list").addEventListener("click", async (ev) => {
      const del = ev.target.closest("[data-del-repo]"), delWs = ev.target.closest("[data-del-ws]"), repo = ev.target.closest("[data-repo]");
      if (del) { if (await dialog("Remove repository", "Remove it from the workspace? Files on disk are not touched.", { ok: "Remove", danger: true })) { await api("DELETE", "/api/workspaces/" + del.dataset.ws + "/repos/" + del.dataset.delRepo).catch((e) => toast("bad", e.message)); await loadWorkspaces(); await refreshAll(); } return; }
      if (delWs) { if (await dialog("Remove workspace", "Remove this workspace and its repository list? Files on disk are not touched.", { ok: "Remove", danger: true })) { await api("DELETE", "/api/workspaces/" + delWs.dataset.delWs).catch((e) => toast("bad", e.message)); await loadWorkspaces(); await refreshAll(); } return; }
      if (repo && !repo.classList.contains("missing")) selectRepo(repo.dataset.repo);
    });
    $("#workspace-list").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target.dataset.repo) selectRepo(ev.target.dataset.repo); });
    $("#btn-add-workspace").addEventListener("click", async () => { const name = await dialog("New workspace", "", { input: "Name", ok: "Create" }); if (name) { try { await api("POST", "/api/workspaces", { name }); await loadWorkspaces(); toast("ok", "Workspace created"); } catch (e) { toast("bad", e.message); } } });
    $("#btn-add-repo").addEventListener("click", async () => {
      if (!S.workspaces.length) { toast("warn", "Create a workspace first."); return; }
      const wid = $("#workspace-select").value || S.workspaces[0].id;
      const path = await dialog("Add repository", "Absolute path to a folder inside a git repository on this machine.", { input: "Path", ok: "Add" });
      if (path) { try { const r = await api("POST", "/api/workspaces/" + wid + "/repos", { path }); await loadWorkspaces(); toast("ok", "Added " + r.name); selectRepo(r.id); } catch (e) { toast("bad", e.message, true); } }
    });
    ["fetch", "pull", "push"].forEach((k) => $("#btn-" + k).addEventListener("click", async () => {
      if (!S.repoId) return;
      const body = {}; if (k === "push" && S.state && !S.state.upstream) { if (!(await dialog("Push", "No upstream is set. Push to origin and set it as upstream?", { ok: "Push" }))) return; body.set_upstream = true; }
      const res = await runJob(k, body, k[0].toUpperCase() + k.slice(1)); if (res) toast("ok", k + " done" + (res.output ? ": " + res.output.split("\n").slice(-1)[0] : "")); await refreshGraph();
    }));
    $("#btn-terminal").addEventListener("click", openTerminal);
    $("#btn-credentials").addEventListener("click", showCredentials);
    $("#btn-help").addEventListener("click", () => $("#help").showModal());
    $$(".right .tab").forEach((t) => t.addEventListener("click", () => { if (t.dataset.tab === "credentials") showCredentials(); else selectTab("right", "detail"); }));
    $("#bottom-tabs").addEventListener("click", (ev) => { const t = ev.target.closest(".tab"); if (t && !ev.target.classList.contains("close")) selectTab("bottom", t.dataset.tab); });
    $("#branch-list").addEventListener("click", async (ev) => {
      const del = ev.target.closest("[data-del-branch]"); const li = ev.target.closest("[data-branch]");
      if (del) { const name = del.dataset.delBranch; if (await dialog("Delete branch", "Delete local branch " + name + "?", { ok: "Delete", danger: true })) { try { await api("DELETE", rp("/branches/" + encodeURIComponent(name))); toast("ok", "Deleted " + name); } catch (e) { if (e.status === 409 && /not fully merged/.test(e.message) && await dialog("Branch not merged", e.message + " Delete anyway?", { ok: "Force delete", danger: true })) { await api("DELETE", rp("/branches/" + encodeURIComponent(name) + "?force=1")).catch((x) => toast("bad", x.message)); } else toast("bad", e.message); } await refreshAll(); } return; }
      if (li) { const name = li.dataset.branch; if (S.state && S.state.branch === name) return; try { await api("POST", rp("/checkout"), { name }); toast("ok", "Checked out " + name); await refreshAll(); } catch (e) { toast("bad", e.message, true); } }
    });
    $("#branch-list").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target.dataset.branch) ev.target.click(); });
    $("#remote-branch-list").addEventListener("click", async (ev) => { const li = ev.target.closest("[data-remote-branch]"); if (!li) return; const name = li.dataset.remoteBranch.split("/").slice(1).join("/"); if (await dialog("Checkout remote branch", "Create local branch " + name + " tracking " + li.dataset.remoteBranch + "?", { ok: "Checkout" })) { try { await api("POST", rp("/branches"), { name, from: li.dataset.remoteBranch }); await api("POST", rp("/checkout"), { name }); toast("ok", "Checked out " + name); await refreshAll(); } catch (e) { toast("bad", e.message, true); } } });
    $("#btn-new-branch").addEventListener("click", async () => { if (!S.repoId) return; const name = await dialog("New branch", "Created from the current HEAD and checked out.", { input: "Branch name", ok: "Create" }); if (!name) return; try { await api("POST", rp("/branches"), { name }); await api("POST", rp("/checkout"), { name }); toast("ok", "On " + name); await refreshAll(); } catch (e) { toast("bad", e.message, true); } });
    const changes = $(".left");
    changes.addEventListener("change", async (ev) => { const cb = ev.target.closest("[data-toggle]"); if (!cb) return; const path = cb.dataset.path, area = cb.dataset.toggle; try { await api("POST", rp(area === "index" ? "/unstage" : "/stage"), { paths: [path] }); } catch (e) { toast("bad", e.message); } await refreshState(); if (S.selection && S.selection.type === "file" && S.selection.path === path) showFileDiff(area === "index" ? "worktree" : "index", path); });
    changes.addEventListener("click", async (ev) => {
      const diff = ev.target.closest("[data-diff]"); if (diff) { showFileDiff(diff.dataset.diff, diff.dataset.path); return; }
      const discard = ev.target.closest("[data-discard]"); if (discard) { const p = discard.dataset.discard; if (await dialog("Discard changes", "Discard all changes in " + p + "? Untracked files are deleted. This cannot be undone.", { ok: "Discard", danger: true })) { try { await api("POST", rp("/discard"), { paths: [p] }); toast("ok", "Discarded " + p); } catch (e) { toast("bad", e.message); } await refreshState(); if (S.selection && S.selection.path === p) { S.selection = null; renderDetail(); } } }
    });
    $("#btn-stage-all").addEventListener("click", async () => { const paths = $$("#unstaged-list [data-path]").map((e) => e.dataset.path); if (paths.length) { await api("POST", rp("/stage"), { paths }).catch((e) => toast("bad", e.message)); await refreshState(); } });
    $("#btn-unstage-all").addEventListener("click", async () => { const paths = $$("#staged-list [data-path]").map((e) => e.dataset.path); if (paths.length) { await api("POST", rp("/unstage"), { paths }).catch((e) => toast("bad", e.message)); await refreshState(); } });
    $("#commit-form").addEventListener("submit", async (ev) => {
      ev.preventDefault(); const msg = $("#commit-message").value, err = $("#commit-error"); err.textContent = "";
      if (!msg.trim()) { err.textContent = "Write a commit message."; $("#commit-message").focus(); return; }
      setBusy("commit", true);
      try { const r = await api("POST", rp("/commit"), { message: msg, amend: $("#commit-amend").checked }); toast("ok", "Committed " + r.sha.slice(0, 10)); $("#commit-message").value = ""; $("#commit-amend").checked = false; await refreshAll(); }
      catch (e) { err.textContent = e.message; } finally { setBusy("commit", false); }
    });
    $("#commit-filter").addEventListener("input", (ev) => { S.filter = ev.target.value.trim().toLowerCase(); renderGraph(); });
    $("#graph").addEventListener("click", (ev) => { const r = ev.target.closest("[data-sha]"); if (r) showCommit(r.dataset.sha); });
    $("#graph").addEventListener("keydown", (ev) => { const r = ev.target.closest("[data-sha]"); if (!r) return; if (ev.key === "Enter") showCommit(r.dataset.sha); if (ev.key === "ArrowDown" && r.nextElementSibling) { ev.preventDefault(); r.nextElementSibling.focus(); } if (ev.key === "ArrowUp" && r.previousElementSibling && r.previousElementSibling.dataset.sha) { ev.preventDefault(); r.previousElementSibling.focus(); } });
    $("#btn-init").addEventListener("click", async () => { const cmd = await dialog("Initialize Argonauta store", "Optional: the command that runs this repository's tests (for example: python -m pytest -q). You can set it later.", { input: "Test command", ok: "Initialize" }); if (cmd === null) return; try { await api("POST", rp("/init"), { test_command: cmd }); toast("ok", "Store created in .argonauta/"); await refreshAll(); } catch (e) { toast("bad", e.message, true); } });
    $("#btn-index").addEventListener("click", async () => { const r = await runJob("index", {}, "Indexing"); if (r) toast("ok", "Indexed " + r.file_count + " files"); });
    $("#btn-test").addEventListener("click", async () => { const r = await runJob("test", {}, "Running tests"); if (r) { toast(r.test_run.status === "passed" ? "ok" : "bad", "Tests " + r.test_run.status + " (exit " + r.test_run.exit_code + ")", r.test_run.status !== "passed"); await refreshAssistant(); } });
    $("#propose-form").addEventListener("submit", async (ev) => {
      ev.preventDefault(); const task = $("#task").value, err = $("#propose-error"); err.textContent = "";
      if (!task.trim()) { err.textContent = "Describe the change you want."; $("#task").focus(); return; }
      const r = await runJob("propose", { task, dry_run: $("#dry-run").checked }, "Proposing"); if (!r) return;
      if (r.manual) { toast("warn", r.message, true); return; }
      if (r.dry_run) { toast("info", "Would send " + r.context.length + " files: " + r.context.join(", "), true); return; }
      $("#task").value = ""; await refreshAssistant(); showProposal(r.proposal.id);
      toast(r.proposal.status === "invalid" ? "bad" : "ok", "Proposal #" + r.proposal.id + " " + r.proposal.status + (r.proposal.notes ? ": " + r.proposal.notes : ""), r.proposal.status === "invalid");
    });
    $("#proposals-table").addEventListener("click", (ev) => { const tr = ev.target.closest("[data-prop]"); if (tr) showProposal(Number(tr.dataset.prop)); });
    $("#proposals-table").addEventListener("keydown", (ev) => { const tr = ev.target.closest("[data-prop]"); if (tr && ev.key === "Enter") showProposal(Number(tr.dataset.prop)); });
    $("#pane-detail").addEventListener("click", async (ev) => {
      const b = ev.target.closest("button"); if (!b) return;
      if (b.dataset.propApply) { const pid = Number(b.dataset.propApply); const r = await runJob("apply", { proposal_id: pid, test: $("#apply-test").checked, commit: $("#apply-commit").checked }, "Applying #" + pid); if (r) { if (r.test_run && r.test_run.status !== "passed") toast("bad", "Tests " + r.test_run.status + "; nothing committed. Fix and commit, or revert.", true); else toast("ok", "Applied" + (r.commit_sha ? " and committed " + r.commit_sha.slice(0, 10) : "")); } await refreshAll(); showProposal(pid); return; }
      const act = b.dataset.propReject ? "reject" : b.dataset.propRevert ? "revert" : b.dataset.propCommit ? "commit" : null; if (!act) return;
      const pid = Number(b.dataset.propReject || b.dataset.propRevert || b.dataset.propCommit);
      if (act === "revert" && !(await dialog("Revert proposal", "Undo proposal #" + pid + " (git revert if committed, reverse-apply otherwise)?", { ok: "Revert", danger: true }))) return;
      try { await api("POST", rp("/proposals/" + pid + "/" + act), {}); toast("ok", act + " done"); } catch (e) { toast("bad", e.message, true); }
      await refreshAll(); showProposal(pid);
    });
    document.addEventListener("keydown", (ev) => {
      const tag = (ev.target.tagName || "").toLowerCase(); if (["input", "textarea", "select"].includes(tag) || ev.target.closest(".xterm") || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === "p") { ev.preventDefault(); $("#task").focus(); } if (ev.key === "c") { ev.preventDefault(); $("#commit-message").focus(); } if (ev.key === "t") { ev.preventDefault(); openTerminal(); } if (ev.key === "/") { ev.preventDefault(); $("#commit-filter").focus(); } if (ev.key === "?") { ev.preventDefault(); $("#help").showModal(); }
    });
    document.addEventListener("visibilitychange", () => { if (document.hidden) clearInterval(S.pollTimer); else startPolling(); });
  }
  function startPolling() { clearInterval(S.pollTimer); S.pollTimer = setInterval(() => { if (S.repoId && !document.hidden) refreshState(); }, 5000); }

  // ----- boot ------------------------------------------------------------------------------
  async function boot() {
    S.token = readToken();
    if (!S.token) { document.body.innerHTML = '<main class="pane"><h1>Argonauta</h1><p class="empty">This page needs the one-time link printed by <code>argonauta ui</code>. Copy that URL (with its #token) into this tab.</p></main>'; return; }
    wire();
    try { await loadWorkspaces(); } catch (err) { toast("bad", err.status === 401 ? "The token is not valid for this server. Re-open the URL printed by argonauta ui." : err.message, true); return; }
    await refreshAll(); startPolling();
  }
  boot();
})();
```

- [ ] **Step 5: Update the static test and run everything**

In `tests/test_api.py::test_index_and_static_served_without_token` change the `/app.js` line to `assert api.get("/app.js", token=None, origin=None, raw=True)[0] == 200` and add `assert api.get("/vendor/xterm.js", token=None, origin=None, raw=True)[0] == 200`.

Run: `.venv/bin/python -m pytest -q && scripts/build.sh && unzip -l dist/argonauta-0.1.0-py3-none-any.whl | grep -c "web/static"`
Expected: all PASS; the wheel lists index.html, app.js, app.css and the five vendor files (count 8).

- [ ] **Step 6: Manual verification in the browser**

Run `scripts/dev.sh ui` from the argonauta repository, then in Chrome (through the claude-in-chrome tools or by hand):

1. The page loads with the workspace "Default" and this repository selected; the graph shows the existing commits with `main` and `origin/main` refs and a signature badge on each row.
2. Create a file, see it under Unstaged, tick it (moves to Staged), click it (diff on the right), write a message, Commit: toast with the sha, graph gains a row.
3. "+ Branch" → `ui-test`, checkout, delete it from the list (confirmation).
4. Terminal button opens a tab with a prompt; `git status` works; closing the tab kills it.
5. Credentials tab shows the SSH identity for `github-personal`, agent status, the GPG key and HEAD signature "good".
6. Initialize → Index → Propose (with `ARGONAUTA_PROVIDER=scripted` and `ARGONAUTA_SCRIPTED_DIFF=examples/sample_reply.md` exported in the shell, against a scratch copy of `examples/sample_project` added as a second repository) → proposal appears, Apply runs tests and commits → Revert.
7. Toggle the OS theme: both light and dark render with readable contrast; Tab moves through every control with a visible ring; `?` opens help; Esc closes it.

Fix anything that does not match, re-run the suite, then commit.

- [ ] **Step 7: Commit**

```bash
git add argonauta/web/static pyproject.toml tests/test_api.py
git commit -m "Add the web UI page, styles, app logic and vendored xterm.js"
```

---

### Task 12: Documentation, scripts and final verification

**Files:**
- Modify: `README.md`, `.env.example`, `argonauta/config.py` (allow `ARGONAUTA_CONFIG_DIR` from `.env`), `docs/superpowers/specs/2026-10-22-argonauta-web-ui-design.md` (note the lane computation moved server-side)

- [ ] **Step 1: README sections**

Add after "The core loop":

```markdown
## Web UI

`argonauta ui` starts a local server on 127.0.0.1, prints a one-time URL with a
token and opens it in your browser. The page has the GitKraken-style layout:
workspaces and branches on the left, the commit graph in the middle, diffs and
details on the right, and proposals, events and terminals at the bottom.

What you can do there: stage, unstage and discard files; commit (with amend);
create, switch and delete local branches; fetch, pull and push; open terminal
tabs; read the credentials panel; and run the whole assistant loop (initialize,
index, propose, apply with tests, commit, reject, revert).

Shortcuts: `p` proposal field, `c` commit message, `t` new terminal, `/`
filter commits, `?` help, `Esc` closes dialogs. Every control is reachable with
Tab and has a visible focus ring; status is always shown with a symbol, never
colour alone; the theme follows your OS and both themes meet WCAG AA contrast.

Stop the server with Ctrl+C. Nothing runs when it is not running.

## Workspaces

A workspace is a named group of repositories. The list lives in
`~/.config/argonauta/workspaces.json` (override with `ARGONAUTA_CONFIG_DIR`).
Each repository keeps its own `.argonauta/` store. Manage them in the page or
with `argonauta workspace list | add PATH | remove PATH`. Running
`argonauta ui` inside an unregistered repository adds it to "Default".

## Credentials panel

Shows the git identity (`user.name`, `user.email`) and where it comes from, the
SSH host alias your remote resolves to, which identity files `ssh -G` would try,
their type and fingerprint, whether the key is loaded in `ssh-add -l`, the
commit-signing key (GPG or SSH) with its validity, and the signature status of
HEAD and of every commit in the graph. It reads `git config`, `ssh -G`,
`ssh-keygen -l` on `.pub` files, `ssh-add -l` and `gpg --list-keys`. It never
opens a private key.

## Embedded terminal

A terminal tab is a real login shell started inside the repository with your
normal environment. It is wired to the page through a WebSocket on the same
loopback server. This is the most sensitive feature in Argonauta: anyone who
can reach the server with the token can run commands as you. The protections
are that the server listens only on 127.0.0.1, every WebSocket must present the
per-run token as its first message and a matching Origin, shells die when the
tab or the server closes, and the server only exists while `argonauta ui` runs.
If you do not want this at all, do not open terminal tabs; nothing is spawned
until you do.
```

Update the "Permissions" list (network: add "the page only talks to 127.0.0.1"), "Data location" (add `~/.config/argonauta/workspaces.json`), and "Limitations" (add merge, rebase, conflict resolution, stash, hunk staging, tags, submodules, and "the web UI is single-user and must never be exposed beyond localhost").

- [ ] **Step 2: Spec note, env example, config allowlist**

Append to the spec's "Front end" section: "Implementation note: lane assignment runs server-side in `web/graph.py` so it is unit-tested in Python; the page only draws." Add to `.env.example`:

```
# --- Web UI ------------------------------------------------------------------
# Where the workspaces registry lives. Default: ~/.config/argonauta
ARGONAUTA_CONFIG_DIR=
```

Add `"ARGONAUTA_CONFIG_DIR"` to `DOTENV_ALLOWLIST` in `argonauta/config.py` (it only chooses where a JSON list is stored; nothing is executed from it). Add to `tests/test_config.py::test_dotenv_only_loads_allowlisted_keys` a line `ARGONAUTA_CONFIG_DIR=/tmp/x` in the `.env` content and assert `"ARGONAUTA_CONFIG_DIR" in loaded`.

- [ ] **Step 3: Final verification**

Run: `scripts/test.sh && scripts/build.sh && scripts/run.sh --version && scripts/demo.sh | tail -3`
Expected: all tests pass, wheel builds, the production run prints the version, the demo completes.

- [ ] **Step 4: Commit**

```bash
git add README.md .env.example argonauta/config.py tests/test_config.py docs/superpowers/specs/2026-10-22-argonauta-web-ui-design.md
git commit -m "Document the web UI, workspaces, credentials panel and embedded terminal"
```
