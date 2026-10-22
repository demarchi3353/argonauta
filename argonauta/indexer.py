"""Lexical index over the repository: file metadata plus a term-frequency
inverted index in SQLite, queried with BM25. No embeddings, no network.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from argonauta.db import Store
from argonauta.gitops import ls_files
from argonauta.models import IndexedFile

MAX_FILE_BYTES = 512_000
SKIP_PREFIXES = (".argonauta/", ".git/", ".worktrees/")
SKIP_DIR_PARTS = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "vendor", "third_party", "thirdparty"}
SKIP_NAMES = {".env"}
SKIP_STEMS = {"license", "licence", "copying", "notice"}  # legal boilerplate pollutes retrieval

LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".swift": "swift", ".sh": "shell", ".bash": "shell",
    ".zsh": "shell", ".md": "markdown", ".rst": "rst", ".txt": "text", ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".html": "html", ".css": "css",
    ".sql": "sql", ".ini": "ini", ".cfg": "ini", ".xml": "xml",
}

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "not", "but", "you", "your",
    "def", "class", "return", "import", "self", "none", "true", "false", "int", "str", "if",
    "else", "elif", "while", "in", "is", "or", "as", "to", "of", "it", "on", "be", "an", "at",
    "by", "we", "do", "no", "so", "up", "my", "me", "all", "any", "can", "use", "new", "add",
    "fix", "make", "should", "when", "then", "than", "into", "over", "also", "only", "just",
    "const", "let", "var", "function", "void", "public", "private", "static", "null",
}

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def tokenize(text: str) -> list[str]:
    """Split text into lowercase identifier-ish tokens, expanding camelCase and snake_case."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        lowered = raw.lower()
        if len(lowered) >= 2 and lowered not in STOPWORDS and not lowered.isdigit():
            out.append(lowered)
        if "_" in raw or any(c.isupper() for c in raw[1:]):
            for part in raw.split("_"):
                for sub in _CAMEL_RE.findall(part):
                    sub = sub.lower()
                    if len(sub) >= 2 and sub != lowered and sub not in STOPWORDS and not sub.isdigit():
                        out.append(sub)
    return out


def language_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in LANGUAGES:
        return LANGUAGES[suffix]
    name = Path(path).name
    if name in ("Makefile", "Dockerfile"):
        return name.lower()
    return ""


def should_skip(rel_path: str) -> bool:
    if rel_path.startswith(SKIP_PREFIXES):
        return True
    parts = rel_path.split("/")
    if any(p in SKIP_DIR_PARTS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name in SKIP_NAMES or name.startswith(".env."):
        return True
    return name.split(".")[0].lower() in SKIP_STEMS


def is_binary(sample: bytes) -> bool:
    return b"\0" in sample


@dataclass
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0


def candidate_files(repo_root: Path) -> list[str]:
    return [p for p in ls_files(repo_root) if not should_skip(p)]


def index_repo(store: Store, repo_root: Path, progress: Callable[[int, int], None] | None = None) -> IndexStats:
    stats = IndexStats()
    known = store.file_hashes()
    seen: set[str] = set()
    paths = candidate_files(repo_root)
    total = len(paths)
    for n, rel in enumerate(paths, start=1):
        if progress:
            progress(n, total)
        stats.scanned += 1
        abs_path = repo_root / rel
        try:
            if not abs_path.is_file() or abs_path.is_symlink():
                stats.skipped += 1
                continue
            size = abs_path.stat().st_size
            if size > MAX_FILE_BYTES:
                stats.skipped += 1
                continue
            data = abs_path.read_bytes()
        except OSError:
            stats.skipped += 1
            continue
        if is_binary(data[:8192]):
            stats.skipped += 1
            continue
        seen.add(rel)
        digest = hashlib.sha256(data).hexdigest()
        if rel in known and known[rel][1] == digest:
            stats.unchanged += 1
            continue
        text = data.decode("utf-8", errors="replace")
        counts = Counter(tokenize(text))
        counts.update(tokenize(rel.replace("/", " ")))
        store.upsert_file(
            path=rel,
            size=size,
            sha256=digest,
            language=language_for(rel),
            line_count=text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            terms=dict(counts),
        )
        stats.indexed += 1
    gone = [p for p in known if p not in seen]
    stats.removed = store.delete_files(gone)
    return stats


@dataclass(frozen=True)
class SearchHit:
    file: IndexedFile
    score: float


def search(store: Store, query: str, limit: int = 10) -> list[SearchHit]:
    terms = tokenize(query)
    if not terms:
        return []
    postings = store.term_postings(terms)
    if not postings:
        return []
    lengths = store.file_lengths()
    n_docs = max(len(lengths), 1)
    avg_len = (sum(lengths.values()) / n_docs) if lengths else 1.0
    k1, b = 1.5, 0.75
    scores: dict[int, float] = {}
    query_counts = Counter(terms)
    for term, plist in postings.items():
        df = len(plist)
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        weight = idf * query_counts[term]
        for file_id, tf in plist:
            dl = lengths.get(file_id, 1)
            denom = tf + k1 * (1 - b + b * dl / avg_len)
            scores[file_id] = scores.get(file_id, 0.0) + weight * (tf * (k1 + 1)) / denom
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    files = store.files_by_id([fid for fid, _ in ranked])
    return [SearchHit(file=files[fid], score=score) for fid, score in ranked if fid in files]
