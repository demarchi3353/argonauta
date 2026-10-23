"""Read-only wrapper around the user's own `gh` CLI for pull requests.

This is the only module that ever invokes `gh`. Safety rules:
- `gh` runs without a shell, from a fixed argv list; the only caller-supplied
  values are a validated positive integer PR number and `--repo owner/repo`
  derived from the `origin` remote.
- The environment is an allow-list (see `gh_env`); token variables such as
  GH_TOKEN, GITHUB_TOKEN and GH_ENTERPRISE_TOKEN are never passed on, so `gh`
  uses only the login stored by `gh auth login`.
- Only read commands are used: `api user`, `pr list/view/diff`
  and GET requests to `repos/...`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from argonauta.gitops import run_git

GH_TIMEOUT = 60
ENV_ALLOW = ("HOME", "PATH", "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "GH_HOST")
INSTALL_HINT = "The GitHub CLI (gh) is not installed. Install it from https://cli.github.com, run `gh auth login`, or set ARGONAUTA_GH_BIN to the binary."
LOGIN_HINT = "gh is not logged in. Run `gh auth login` in a terminal."

_PART = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
# git@host-or-ssh-alias:owner/repo(.git)   (an alias cannot be resolved without ssh config, so any host is accepted)
_SCP_RE = re.compile(rf"^[\w.-]+@[\w.-]+:(?P<owner>{_PART})/(?P<repo>{_PART}?)(?:\.git)?/?$")
# ssh://git@host[:port]/owner/repo(.git)
_SSH_RE = re.compile(rf"^ssh://[\w.-]+@[\w.-]+(?::\d+)?/(?P<owner>{_PART})/(?P<repo>{_PART}?)(?:\.git)?/?$")
# https://github.com/owner/repo(.git), optionally with user@ in front of the host
_HTTPS_RE = re.compile(rf"^https://(?:[^@/\s]+@)?github\.com/(?P<owner>{_PART})/(?P<repo>{_PART}?)(?:\.git)?/?$")


class GitHubError(RuntimeError):
    """A failure with a message that is safe to show the user."""


def find_gh_bin() -> str | None:
    override = os.environ.get("ARGONAUTA_GH_BIN", "").strip()
    if override:
        # a bare name ("gh2") resolves through PATH; a path is kept as given
        return override if os.sep in override else (shutil.which(override) or override)
    return shutil.which("gh")


def gh_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}


def _run(repo_root: Path | None, args: list[str]) -> str:
    binary = find_gh_bin()
    if not binary:
        raise GitHubError(INSTALL_HINT)
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
            timeout=GH_TIMEOUT, env=gh_env(), cwd=repo_root,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise GitHubError(INSTALL_HINT) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(f"gh {args[0]} timed out after {GH_TIMEOUT} seconds") from exc
    except OSError as exc:
        raise GitHubError(f"gh {args[0]} failed: {exc.strerror or exc}") from exc
    if proc.returncode != 0:
        raise GitHubError(f"gh {args[0]} failed: {(proc.stderr or '').strip()[-300:]}")
    return proc.stdout


def _run_json(repo_root: Path | None, args: list[str]):
    try:
        return json.loads(_run(repo_root, args))
    except json.JSONDecodeError as exc:
        raise GitHubError("gh returned something that is not JSON") from exc


def _run_json_pages(repo_root: Path, args: list[str]) -> list:
    """`gh api --paginate` prints one JSON array per page back to back; join them."""
    text, items, pos, decoder = _run(repo_root, args), [], 0, json.JSONDecoder()
    try:
        while True:
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text):
                return items
            page, pos = decoder.raw_decode(text, pos)
            items.extend(page if isinstance(page, list) else [page])
    except json.JSONDecodeError as exc:
        raise GitHubError("gh returned something that is not JSON") from exc


def gh_status() -> dict:
    """{installed, logged_in, user, error} for the settings dialog; never raises."""
    info: dict = {"installed": False, "logged_in": None, "user": None, "error": None}
    binary = find_gh_bin()
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        info["error"] = INSTALL_HINT
        return info
    info["installed"] = True
    # `gh auth status` fails when any stored account is broken, even if the active one works;
    # `gh api user` uses the active account, which is what every other call here uses too.
    try:
        user = _run(None, ["api", "user", "--jq", ".login"]).strip().strip('"')
    except GitHubError:
        user = ""
    if not user:
        info["logged_in"] = False
        info["error"] = LOGIN_HINT
        return info
    info["logged_in"] = True
    info["user"] = user
    return info


def origin_slug(repo_root: Path) -> str:
    proc = run_git(repo_root, "remote", "get-url", "origin", check=False)
    url = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    if not url:
        raise GitHubError("This repository has no `origin` remote, so there is no GitHub repository to read pull requests from.")
    for pattern in (_SCP_RE, _SSH_RE, _HTTPS_RE):
        m = pattern.match(url)
        if m:
            return f"{m['owner']}/{m['repo']}"
    raise GitHubError("The `origin` remote does not look like a GitHub repository.")


def _number(number: int) -> str:
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise GitHubError("Invalid pull request number.")
    return str(number)


def _expect(value, kind: type):
    if not isinstance(value, kind):
        raise GitHubError("unexpected gh output")
    return value


def _items(values) -> list[dict]:
    if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
        raise GitHubError("unexpected gh output")
    return values


def _login(user) -> str:
    return (user or {}).get("login", "") if isinstance(user, dict) else ""


def list_prs(repo_root: Path) -> list[dict]:
    data = _run_json(repo_root, [
        "pr", "list", "--repo", origin_slug(repo_root), "--state", "open", "--limit", "50",
        "--json", "number,title,author,headRefName,updatedAt",
    ])
    return [{**pr, "author": _login(pr.get("author"))} for pr in _items(data)]


def pr_detail(repo_root: Path, number: int) -> dict:
    n = _number(number)
    pr = _run_json(repo_root, [
        "pr", "view", n, "--repo", origin_slug(repo_root),
        "--json", "number,title,body,author,baseRefName,headRefName,url",
    ])
    _expect(pr, dict)
    return {**pr, "author": _login(pr.get("author"))}


def pr_diff(repo_root: Path, number: int) -> str:
    n = _number(number)
    return _run(repo_root, ["pr", "diff", n, "--repo", origin_slug(repo_root)])


def pr_comments(repo_root: Path, number: int) -> list[dict]:
    """Inline review comments, review summaries and conversation comments, oldest first."""
    n = _number(number)
    slug = origin_slug(repo_root)
    out: list[dict] = []
    for c in _items(_run_json_pages(repo_root, ["api", f"repos/{slug}/pulls/{n}/comments", "--paginate"])):
        out.append({
            "author": _login(c.get("user")), "body": c.get("body") or "", "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"), "hunk": c.get("diff_hunk"),
            "created_at": c.get("created_at") or "",
        })
    for r in _items(_run_json_pages(repo_root, ["api", f"repos/{slug}/pulls/{n}/reviews", "--paginate"])):
        if (r.get("body") or "").strip():
            out.append({
                "author": _login(r.get("user")), "body": r["body"], "path": None,
                "line": None, "hunk": None, "created_at": r.get("submitted_at") or "",
            })
    for c in _items(_run_json_pages(repo_root, ["api", f"repos/{slug}/issues/{n}/comments", "--paginate"])):
        out.append({
            "author": _login(c.get("user")), "body": c.get("body") or "", "path": None,
            "line": None, "hunk": None, "created_at": c.get("created_at") or "",
        })
    return sorted(out, key=lambda c: c["created_at"])
