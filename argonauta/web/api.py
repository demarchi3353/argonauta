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
from argonauta.workspaces import RegistryError, Repo

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
