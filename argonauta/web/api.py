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


# ----- assistant --------------------------------------------------------------------------
from argonauta import workflow  # noqa: E402
from argonauta.providers import ProviderUnavailable  # noqa: E402
from argonauta.runner import RunnerError, parse_command  # noqa: E402
from argonauta.web import credentials as credentials_mod  # noqa: E402
from argonauta.web.terminal import TerminalError  # noqa: E402


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

    task = ""
    dry_run = False
    pid = 0
    pid_opt: int | None = None
    run_tests = do_commit = True
    if kind == "propose":
        task = _wf(lambda: workflow.validate_task(require_str(body, "task", workflow.MAX_TASK_CHARS)))
        dry_run = require_bool(body, "dry_run", False)
    elif kind == "apply":
        raw_pid = body.get("proposal_id")
        if not isinstance(raw_pid, int) or isinstance(raw_pid, bool) or raw_pid <= 0:
            raise ApiError(400, "proposal_id must be a positive integer")
        pid = raw_pid
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


# ----- user-level model settings ------------------------------------------------------------
from argonauta.config import ConfigError, local_setting_sources  # noqa: E402
from argonauta.user_settings import UserSettingsError, load_user_settings, save_user_settings, validate_local_url  # noqa: E402


def _model_settings_payload(ctx: Any) -> dict:
    import os

    try:
        settings = load_settings(Path.cwd())
    except ConfigError as exc:
        raise ApiError(400, str(exc)) from exc
    return {
        "effective_provider": settings.effective_provider,
        "provider": describe_provider(settings),
        "anthropic_key_present": settings.has_api_key,
        "local_url": settings.local_url,
        "local_model": settings.local_model,
        "local_allow_remote": settings.local_allow_remote,
        "local_api_key_present": bool(os.environ.get("ARGONAUTA_LOCAL_API_KEY", "").strip()),
        "sources": local_setting_sources(),
    }


@route("GET", r"^/api/settings/model$")
def get_model_settings(ctx, params, query, body):
    return 200, _model_settings_payload(ctx)


@route("PUT", r"^/api/settings/model$")
def put_model_settings(ctx, params, query, body):
    values = {
        "local_url": require_str(body, "local_url", 2000, allow_empty=True),
        "local_model": require_str(body, "local_model", 200, allow_empty=True),
        "local_allow_remote": require_bool(body, "local_allow_remote", False),
    }
    try:
        with ctx.lock:
            save_user_settings(values)
    except UserSettingsError as exc:
        raise ApiError(400, str(exc)) from exc
    log.info("model settings updated")
    return 200, _model_settings_payload(ctx)


@route("POST", r"^/api/settings/model/test$")
def test_model_connection(ctx, params, query, body):
    import json as _json
    import urllib.error
    import urllib.request

    url = require_str(body, "local_url", 2000)
    allow_remote = require_bool(body, "local_allow_remote", False)
    try:
        base = validate_local_url(url, allow_remote)
    except UserSettingsError as exc:
        raise ApiError(400, str(exc)) from exc
    request = urllib.request.Request(base + "/models", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - scheme validated above
            data = _json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return 200, {"ok": False, "error": f"the server answered HTTP {exc.code} for {base}/models", "models": []}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return 200, {"ok": False, "error": f"could not reach {base}: {getattr(exc, 'reason', exc)}", "models": []}
    items = data.get("data") if isinstance(data, dict) else None
    models = [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)] if isinstance(items, list) else []
    return 200, {"ok": True, "error": None, "models": models[:200]}
