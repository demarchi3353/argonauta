"""User-level registry of workspaces (named groups of repositories).

Stored outside any repository at ~/.config/argonauta/workspaces.json so the
web UI can switch between repositories. Only paths that resolve to a git
toplevel are accepted, and the API refers to repositories by id only.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
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
    parent_id: str | None = None  # set when this entry is a git worktree of another entry

    @property
    def worktree(self) -> bool:
        return self.parent_id is not None

    @property
    def exists(self) -> bool:
        return (self.path / ".git").exists()

    @property
    def initialized(self) -> bool:
        return (self.path / DATA_DIR_NAME / DB_FILE_NAME).is_file()

    def to_dict(self) -> dict:
        return {"id": self.id, "path": str(self.path), "name": self.name, "exists": self.exists, "initialized": self.initialized, "parent_id": self.parent_id, "worktree": self.worktree}


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
        self._lock = threading.RLock()  # request threads mutate and save concurrently

    # ----- persistence -----
    def _load_unlocked(self) -> None:
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
                parent = r.get("parent_id") if isinstance(r.get("parent_id"), str) else None
                repos.append(Repo(id=r["id"], path=Path(r["path"]), name=r["name"], parent_id=parent))
            out.append(Workspace(id=w["id"], name=w["name"], repos=repos))
        self.workspaces = out

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "workspaces": [
            {"id": w.id, "name": w.name, "repos": [{"id": r.id, "path": str(r.path), "name": r.name, **({"parent_id": r.parent_id} if r.parent_id else {})} for r in w.repos]}
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

    def workspace_of(self, repo_id: str) -> Workspace | None:
        return next((w for w in self.workspaces if any(r.id == repo_id for r in w.repos)), None)

    def find_repo_by_path(self, path: Path) -> Repo | None:
        resolved = path.resolve()
        for w in self.workspaces:
            for r in w.repos:
                if r.path == resolved:
                    return r
        return None

    # ----- mutations -----
    def _add_workspace_unlocked(self, name: str) -> Workspace:
        ws = Workspace(id="w_" + secrets.token_hex(4), name=_clean_name(name))
        self.workspaces.append(ws)
        self.save()
        return ws

    def _rename_workspace_unlocked(self, workspace_id: str, name: str) -> Workspace:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise RegistryError(f"no workspace {workspace_id}")
        ws.name = _clean_name(name)
        self.save()
        return ws

    def _remove_workspace_unlocked(self, workspace_id: str) -> None:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise RegistryError(f"no workspace {workspace_id}")
        self.workspaces.remove(ws)
        self.save()

    def _add_repo_unlocked(self, workspace_id: str, path: Path, parent_id: str | None = None) -> Repo:
        ws = self.get_workspace(workspace_id)
        if ws is None:
            raise RegistryError(f"no workspace {workspace_id}")
        try:
            top = gitops.repo_toplevel(Path(path).expanduser())
        except gitops.GitError as exc:
            raise RegistryError(f"{path} is not inside a git repository") from exc
        if self.find_repo_by_path(top) is not None:
            raise RegistryError(f"{top} is already registered")
        if parent_id is not None and self.get_repo(parent_id) is None:
            raise RegistryError(f"no repository {parent_id}")
        repo = Repo(id="r_" + secrets.token_hex(4), path=top, name=top.name, parent_id=parent_id)
        ws.repos.append(repo)
        self.save()
        return repo

    def _remove_repo_unlocked(self, repo_id: str) -> None:
        for w in self.workspaces:
            for r in list(w.repos):
                if r.id == repo_id:
                    w.repos.remove(r)
                    for child in [c for c in w.repos if c.parent_id == repo_id]:  # its worktree entries go too
                        w.repos.remove(child)
                    self.save()
                    return
        raise RegistryError(f"no repository {repo_id}")

    def _ensure_repo_unlocked(self, path: Path) -> Repo:
        top = gitops.repo_toplevel(Path(path))
        existing = self.find_repo_by_path(top)
        if existing is not None:
            return existing
        default = next((w for w in self.workspaces if w.name == DEFAULT_WORKSPACE), None)
        if default is None:
            default = self.add_workspace(DEFAULT_WORKSPACE)
        return self.add_repo(default.id, top)

    def load(self):
        with self._lock:
            return self._load_unlocked()

    def save(self):
        with self._lock:
            return self._save_unlocked()

    def add_workspace(self, name: str):
        with self._lock:
            return self._add_workspace_unlocked(name)

    def rename_workspace(self, workspace_id: str, name: str):
        with self._lock:
            return self._rename_workspace_unlocked(workspace_id, name)

    def remove_workspace(self, workspace_id: str):
        with self._lock:
            return self._remove_workspace_unlocked(workspace_id)

    def add_repo(self, workspace_id: str, path: Path, parent_id: str | None = None):
        with self._lock:
            return self._add_repo_unlocked(workspace_id, path, parent_id)

    def remove_repo(self, repo_id: str):
        with self._lock:
            return self._remove_repo_unlocked(repo_id)

    def ensure_repo(self, path: Path):
        with self._lock:
            return self._ensure_repo_unlocked(path)


def open_registry() -> Registry:
    registry = Registry(config_dir() / FILE_NAME)
    registry.load()
    return registry
