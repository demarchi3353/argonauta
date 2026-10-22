"""Extract, parse and validate unified diffs coming from untrusted text.

A diff is untrusted whether it came from a model, a file the user pasted, or
an import. Validation never touches the working tree; `git apply --check` is a
separate step in gitops.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_DIFF_BYTES = 1_000_000
FORBIDDEN_PREFIXES = (".git/", ".argonauta/")
FORBIDDEN_NAMES = (".env",)

_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.DOTALL)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


@dataclass
class DiffFile:
    old_path: str | None
    new_path: str | None
    insertions: int = 0
    deletions: int = 0
    hunks: int = 0

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    files: list[DiffFile] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def insertions(self) -> int:
        return sum(f.insertions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)


def extract_diff(text: str) -> str:
    """Pull the unified diff out of a model reply.

    Prefers the largest fenced ```diff block; otherwise starts at the first
    `diff --git` or `--- ` line. Returns '' when nothing diff-like is present.
    """
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)]
    diff_like = [c for c in candidates if _looks_like_diff(c)]
    if diff_like:
        body = max(diff_like, key=len)
    else:
        body = text
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("diff --git ") or (line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")):
            start = i
            break
    if start is None:
        return ""
    out = "\n".join(lines[start:]).rstrip("\n") + "\n"
    return out


def _looks_like_diff(text: str) -> bool:
    return "diff --git " in text or ("\n--- " in "\n" + text and "\n+++ " in "\n" + text)


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path in ("/dev/null", ""):
        return None
    path = path.split("\t")[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_diff(diff_text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: DiffFile | None = None
    header_done = True
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DIFF_GIT_RE.match(line)
        if m:
            current = DiffFile(old_path=m.group(1), new_path=m.group(2))
            files.append(current)
            header_done = False
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _strip_prefix(line[4:])
            new = _strip_prefix(lines[i + 1][4:])
            if current is None or header_done:
                # Plain unified diff section without a `diff --git` line.
                current = DiffFile(old_path=old, new_path=new)
                files.append(current)
            else:
                current.old_path = old
                current.new_path = new
            header_done = True
            i += 2
            continue
        if current is not None:
            if line.startswith("new file mode"):
                current.old_path = None
            elif line.startswith("deleted file mode"):
                current.new_path = None
            elif _HUNK_RE.match(line):
                current.hunks += 1
                header_done = True
            elif current.hunks > 0:
                if line.startswith("+") and not line.startswith("+++ "):
                    current.insertions += 1
                elif line.startswith("-") and not line.startswith("--- "):
                    current.deletions += 1
        i += 1
    return files


def _path_problem(path: str, repo_root: Path | None) -> str | None:
    if not path:
        return "empty path"
    if "\\" in path:
        return f"{path!r}: backslashes are not allowed"
    if posixpath.isabs(path) or path.startswith("/"):
        return f"{path!r}: absolute paths are not allowed"
    parts = path.split("/")
    if any(p in ("..", "") for p in parts):
        return f"{path!r}: path traversal or empty segment"
    if any(path.startswith(pre) for pre in FORBIDDEN_PREFIXES):
        return f"{path!r}: inside a protected directory"
    base = parts[-1]
    if base in FORBIDDEN_NAMES or base.startswith(".env."):
        return f"{path!r}: environment files are protected"
    if repo_root is not None:
        resolved = (repo_root / path).resolve()
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError:
            return f"{path!r}: escapes the repository"
        # Refuse to follow symlinks out of the repo.
        probe = repo_root
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                return f"{path!r}: path goes through a symlink"
    return None


def validate_diff(diff_text: str, repo_root: Path | None = None) -> ValidationResult:
    result = ValidationResult(ok=True)
    if not diff_text.strip():
        return ValidationResult(ok=False, errors=["the diff is empty"])
    if len(diff_text.encode("utf-8", "replace")) > MAX_DIFF_BYTES:
        return ValidationResult(ok=False, errors=[f"the diff is larger than {MAX_DIFF_BYTES} bytes"])
    files = parse_diff(diff_text)
    if not files:
        return ValidationResult(ok=False, errors=["no file headers found (expected `diff --git` or `---`/`+++` lines)"])
    for f in files:
        if f.hunks == 0 and not (f.is_new or f.is_deleted):
            result.errors.append(f"{f.path!r}: no hunks")
        for p in (f.old_path, f.new_path):
            if p is None:
                continue
            problem = _path_problem(p, repo_root)
            if problem:
                result.errors.append(problem)
        if repo_root is not None and not f.is_new and f.old_path and not (repo_root / f.old_path).is_file():
            result.errors.append(f"{f.old_path!r}: file does not exist in the repository")
    result.files = files
    result.ok = not result.errors
    return result
