"""SQLite store. One database per repository, created by `argonauta init`.

All accessors return the dataclasses in models.py. No row ever stores a secret.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from argonauta.models import (
    PROPOSAL_STATUSES,
    REVIEW_MODES,
    REVIEW_SOURCES,
    REVIEW_STATUSES,
    TEST_STATUSES,
    Change,
    Event,
    IndexedFile,
    Proposal,
    Review,
    TestRun,
    utcnow,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT NOT NULL UNIQUE,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    language   TEXT NOT NULL DEFAULT '',
    line_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS terms (
    term    TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    count   INTEGER NOT NULL,
    PRIMARY KEY (term, file_id)
);
CREATE INDEX IF NOT EXISTS idx_terms_file ON terms(file_id);
CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    mode          TEXT NOT NULL,
    source_kind   TEXT NOT NULL,
    source_ref    TEXT NOT NULL DEFAULT '',
    source_label  TEXT NOT NULL DEFAULT '',
    source_diff   TEXT NOT NULL DEFAULT '',
    omitted_files TEXT NOT NULL DEFAULT '[]',
    comments      TEXT NOT NULL DEFAULT '[]',
    draft         TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    findings      TEXT NOT NULL DEFAULT '[]',
    raw           TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER,
    output_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    task          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('proposed','applied','rejected','invalid','reverted')),
    explanation   TEXT NOT NULL DEFAULT '',
    diff          TEXT NOT NULL DEFAULT '',
    context_files TEXT NOT NULL DEFAULT '[]',
    input_tokens  INTEGER,
    output_tokens INTEGER,
    notes         TEXT NOT NULL DEFAULT '',
    is_sample     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    applied_at  TEXT NOT NULL,
    files       TEXT NOT NULL DEFAULT '[]',
    insertions  INTEGER NOT NULL DEFAULT 0,
    deletions   INTEGER NOT NULL DEFAULT 0,
    commit_sha  TEXT,
    reverted_at TEXT,
    is_sample   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS test_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER REFERENCES proposals(id) ON DELETE CASCADE,
    change_id   INTEGER REFERENCES changes(id) ON DELETE SET NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    command     TEXT NOT NULL,
    exit_code   INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('passed','failed','error')),
    output_tail TEXT NOT NULL DEFAULT '',
    is_sample   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ref_table TEXT,
    ref_id    INTEGER,
    message   TEXT NOT NULL
);
"""


class StoreError(RuntimeError):
    """Raised for invalid store operations (unknown id, bad status, ...)."""


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.isolation_level = None  # explicit transactions via _tx()
        self._depth = 0

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Nesting-aware transaction: only the outermost block commits or rolls back."""
        if self._depth == 0:
            self.conn.execute("BEGIN")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.commit()

    # ----- lifecycle -------------------------------------------------------
    @classmethod
    def open(cls, path: Path, create: bool = False) -> Store:
        if not create and not path.is_file():
            raise StoreError(f"No Argonauta store at {path}. Run `argonauta init` first.")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        store = cls(conn)
        store._ensure_schema()
        return store

    @classmethod
    def in_memory(cls) -> Store:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        store = cls(conn)
        store._ensure_schema()
        return store

    def close(self) -> None:
        self.conn.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        with self._tx():
            current = self.get_meta("schema_version")
            if current is None:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
                self.set_meta("created_at", utcnow())
            elif int(current) > SCHEMA_VERSION:
                raise StoreError(
                    f"Store schema {current} is newer than this version supports ({SCHEMA_VERSION})."
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._tx():
            yield self.conn

    # ----- meta / settings -------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self._tx():
            if value is None:
                self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def all_settings(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ----- files / index ---------------------------------------------------
    def list_files(self) -> list[IndexedFile]:
        rows = self.conn.execute("SELECT * FROM files ORDER BY path").fetchall()
        return [IndexedFile.from_row(r) for r in rows]

    def file_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def file_hashes(self) -> dict[str, tuple[int, str]]:
        rows = self.conn.execute("SELECT id, path, sha256 FROM files").fetchall()
        return {r["path"]: (r["id"], r["sha256"]) for r in rows}

    def upsert_file(
        self,
        path: str,
        size: int,
        sha256: str,
        language: str,
        line_count: int,
        terms: dict[str, int],
    ) -> int:
        now = utcnow()
        with self._tx():
            self.conn.execute(
                "INSERT INTO files(path, size, sha256, language, line_count, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET size = excluded.size, sha256 = excluded.sha256, "
                "language = excluded.language, line_count = excluded.line_count, "
                "indexed_at = excluded.indexed_at",
                (path, size, sha256, language, line_count, now),
            )
            file_id = self.conn.execute(
                "SELECT id FROM files WHERE path = ?", (path,)
            ).fetchone()["id"]
            self.conn.execute("DELETE FROM terms WHERE file_id = ?", (file_id,))
            self.conn.executemany(
                "INSERT INTO terms(term, file_id, count) VALUES (?, ?, ?)",
                ((term, file_id, count) for term, count in terms.items()),
            )
        return int(file_id)

    def delete_files(self, paths: Iterable[str]) -> int:
        paths = list(paths)
        if not paths:
            return 0
        with self._tx():
            cur = self.conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])
        return cur.rowcount if cur.rowcount >= 0 else len(paths)

    def term_postings(self, terms: Iterable[str]) -> dict[str, list[tuple[int, int]]]:
        """term -> [(file_id, count), ...]"""
        result: dict[str, list[tuple[int, int]]] = {}
        for term in set(terms):
            rows = self.conn.execute(
                "SELECT file_id, count FROM terms WHERE term = ?", (term,)
            ).fetchall()
            if rows:
                result[term] = [(r["file_id"], r["count"]) for r in rows]
        return result

    def file_lengths(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT file_id, SUM(count) AS total FROM terms GROUP BY file_id"
        ).fetchall()
        return {r["file_id"]: r["total"] for r in rows}

    def files_by_id(self, ids: Iterable[int]) -> dict[int, IndexedFile]:
        ids = list(ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM files WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {r["id"]: IndexedFile.from_row(r) for r in rows}

    def last_indexed_at(self) -> str | None:
        row = self.conn.execute("SELECT MAX(indexed_at) AS m FROM files").fetchone()
        return row["m"]

    # ----- proposals -------------------------------------------------------
    def create_proposal(
        self,
        *,
        task: str,
        provider: str,
        model: str,
        status: str,
        explanation: str = "",
        diff: str = "",
        context_files: list[str] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        notes: str = "",
        is_sample: bool = False,
        created_at: str | None = None,
    ) -> Proposal:
        if status not in PROPOSAL_STATUSES:
            raise StoreError(f"Invalid proposal status {status!r}")
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO proposals(created_at, task, provider, model, status, explanation, "
                "diff, context_files, input_tokens, output_tokens, notes, is_sample) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    created_at or utcnow(),
                    task,
                    provider,
                    model,
                    status,
                    explanation,
                    diff,
                    json.dumps(context_files or []),
                    input_tokens,
                    output_tokens,
                    notes,
                    int(is_sample),
                ),
            )
        return self.get_proposal(int(cur.lastrowid))

    def get_proposal(self, proposal_id: int) -> Proposal:
        row = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise StoreError(f"No proposal with id {proposal_id}.")
        return Proposal.from_row(row)

    def list_proposals(self, limit: int = 50) -> list[Proposal]:
        rows = self.conn.execute(
            "SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Proposal.from_row(r) for r in rows]

    # ----- reviews ---------------------------------------------------------
    def create_review(
        self,
        *,
        mode: str,
        source_kind: str,
        source_ref: str,
        source_label: str,
        source_diff: str,
        omitted_files: list[str],
        comments: list[dict],
        draft: str,
        provider: str,
        model: str,
        status: str,
        summary: str,
        findings: list[dict],
        raw: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        created_at: str | None = None,
    ) -> Review:
        if mode not in REVIEW_MODES:
            raise StoreError(f"Invalid review mode {mode!r}")
        if source_kind not in REVIEW_SOURCES:
            raise StoreError(f"Invalid review source {source_kind!r}")
        if status not in REVIEW_STATUSES:
            raise StoreError(f"Invalid review status {status!r}")
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO reviews(created_at, mode, source_kind, source_ref, source_label, "
                "source_diff, omitted_files, comments, draft, provider, model, status, summary, "
                "findings, raw, input_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    created_at or utcnow(),
                    mode,
                    source_kind,
                    source_ref,
                    source_label,
                    source_diff,
                    json.dumps(omitted_files or []),
                    json.dumps(comments or []),
                    draft,
                    provider,
                    model,
                    status,
                    summary,
                    json.dumps(findings or []),
                    raw,
                    input_tokens,
                    output_tokens,
                ),
            )
        return self.get_review(int(cur.lastrowid))

    def get_review(self, review_id: int) -> Review:
        row = self.conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            raise StoreError(f"No review with id {review_id}.")
        return Review.from_row(row)

    def list_reviews(self, limit: int = 50) -> list[Review]:
        rows = self.conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Review.from_row(r) for r in rows]

    def count_proposals_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def update_proposal_status(self, proposal_id: int, status: str, notes: str | None = None) -> Proposal:
        if status not in PROPOSAL_STATUSES:
            raise StoreError(f"Invalid proposal status {status!r}")
        self.get_proposal(proposal_id)
        with self._tx():
            if notes is None:
                self.conn.execute(
                    "UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id)
                )
            else:
                self.conn.execute(
                    "UPDATE proposals SET status = ?, notes = ? WHERE id = ?",
                    (status, notes, proposal_id),
                )
        return self.get_proposal(proposal_id)

    # ----- changes ---------------------------------------------------------
    def create_change(
        self,
        *,
        proposal_id: int,
        files: list[str],
        insertions: int,
        deletions: int,
        commit_sha: str | None = None,
        is_sample: bool = False,
        applied_at: str | None = None,
    ) -> Change:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO changes(proposal_id, applied_at, files, insertions, deletions, "
                "commit_sha, reverted_at, is_sample) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    proposal_id,
                    applied_at or utcnow(),
                    json.dumps(files),
                    insertions,
                    deletions,
                    commit_sha,
                    int(is_sample),
                ),
            )
        return self.get_change(int(cur.lastrowid))

    def get_change(self, change_id: int) -> Change:
        row = self.conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        if row is None:
            raise StoreError(f"No change with id {change_id}.")
        return Change.from_row(row)

    def change_for_proposal(self, proposal_id: int) -> Change | None:
        row = self.conn.execute(
            "SELECT * FROM changes WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return Change.from_row(row) if row else None

    def list_changes(self, limit: int = 50) -> list[Change]:
        rows = self.conn.execute(
            "SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Change.from_row(r) for r in rows]

    def set_change_commit(self, change_id: int, commit_sha: str) -> Change:
        self.get_change(change_id)
        with self._tx():
            self.conn.execute(
                "UPDATE changes SET commit_sha = ? WHERE id = ?", (commit_sha, change_id)
            )
        return self.get_change(change_id)

    def mark_change_reverted(self, change_id: int) -> Change:
        self.get_change(change_id)
        with self._tx():
            self.conn.execute(
                "UPDATE changes SET reverted_at = ? WHERE id = ?", (utcnow(), change_id)
            )
        return self.get_change(change_id)

    # ----- test runs -------------------------------------------------------
    def create_test_run(
        self,
        *,
        proposal_id: int | None,
        change_id: int | None,
        started_at: str,
        finished_at: str,
        command: str,
        exit_code: int | None,
        status: str,
        output_tail: str,
        is_sample: bool = False,
    ) -> TestRun:
        if status not in TEST_STATUSES:
            raise StoreError(f"Invalid test status {status!r}")
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO test_runs(proposal_id, change_id, started_at, finished_at, command, "
                "exit_code, status, output_tail, is_sample) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id,
                    change_id,
                    started_at,
                    finished_at,
                    command,
                    exit_code,
                    status,
                    output_tail,
                    int(is_sample),
                ),
            )
        return self.get_test_run(int(cur.lastrowid))

    def get_test_run(self, run_id: int) -> TestRun:
        row = self.conn.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"No test run with id {run_id}.")
        return TestRun.from_row(row)

    def list_test_runs(self, proposal_id: int | None = None, limit: int = 50) -> list[TestRun]:
        if proposal_id is None:
            rows = self.conn.execute(
                "SELECT * FROM test_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM test_runs WHERE proposal_id = ? ORDER BY id DESC LIMIT ?",
                (proposal_id, limit),
            ).fetchall()
        return [TestRun.from_row(r) for r in rows]

    def latest_test_run_for_proposal(self, proposal_id: int) -> TestRun | None:
        runs = self.list_test_runs(proposal_id, limit=1)
        return runs[0] if runs else None

    # ----- events ----------------------------------------------------------
    def record_event(
        self, kind: str, message: str, ref_table: str | None = None, ref_id: int | None = None
    ) -> Event:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO events(ts, kind, ref_table, ref_id, message) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), kind, ref_table, ref_id, message),
            )
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Event.from_row(row)

    def list_events(self, limit: int = 100) -> list[Event]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event.from_row(r) for r in rows]

    # ----- bulk ------------------------------------------------------------
    def delete_samples(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self._tx():
            for table in ("test_runs", "changes", "proposals"):
                cur = self.conn.execute(f"DELETE FROM {table} WHERE is_sample = 1")
                counts[table] = cur.rowcount
            cur = self.conn.execute("DELETE FROM events WHERE kind LIKE 'sample.%'")
            counts["events"] = cur.rowcount
        return counts

    def clear_records(self) -> None:
        """Remove every proposal, change, test run and event (keeps index and settings)."""
        with self._tx():
            for table in ("test_runs", "changes", "proposals", "events", "reviews"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('test_runs','changes','proposals','events','reviews')"
            )

    def raw_rows(self, table: str) -> list[dict]:
        if table not in ("proposals", "changes", "test_runs", "events", "files", "settings", "reviews"):
            raise StoreError(f"Cannot export table {table!r}")
        rows = self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]
