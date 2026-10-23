"""Typed rows for the SQLite store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

PROPOSAL_STATUSES = ("proposed", "applied", "rejected", "invalid", "reverted")
TEST_STATUSES = ("passed", "failed", "error")
REVIEW_STATUSES = {"ok", "unparsed", "failed"}
REVIEW_MODES = {"code", "received", "mine"}
REVIEW_SOURCES = {"worktree", "commit", "branch", "pr"}


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class IndexedFile:
    id: int
    path: str
    size: int
    sha256: str
    language: str
    line_count: int
    indexed_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IndexedFile:
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Proposal:
    id: int
    created_at: str
    task: str
    provider: str
    model: str
    status: str
    explanation: str
    diff: str
    context_files: list[str]
    input_tokens: int | None
    output_tokens: int | None
    notes: str
    is_sample: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Proposal:
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            task=row["task"],
            provider=row["provider"],
            model=row["model"],
            status=row["status"],
            explanation=row["explanation"],
            diff=row["diff"],
            context_files=json.loads(row["context_files"] or "[]"),
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            notes=row["notes"] or "",
            is_sample=bool(row["is_sample"]),
        )


@dataclass(frozen=True)
class Review:
    id: int
    created_at: str
    mode: str
    source_kind: str
    source_ref: str
    source_label: str
    source_diff: str
    omitted_files: list[str]
    comments: list[dict]
    draft: str
    provider: str
    model: str
    status: str
    summary: str
    findings: list[dict]
    raw: str
    input_tokens: int | None
    output_tokens: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Review:
        return cls(
            id=row["id"],
            created_at=row["created_at"],
            mode=row["mode"],
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            source_label=row["source_label"],
            source_diff=row["source_diff"],
            omitted_files=json.loads(row["omitted_files"] or "[]"),
            comments=json.loads(row["comments"] or "[]"),
            draft=row["draft"],
            provider=row["provider"],
            model=row["model"],
            status=row["status"],
            summary=row["summary"],
            findings=json.loads(row["findings"] or "[]"),
            raw=row["raw"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
        )


@dataclass(frozen=True)
class Change:
    id: int
    proposal_id: int
    applied_at: str
    files: list[str]
    insertions: int
    deletions: int
    commit_sha: str | None
    reverted_at: str | None
    is_sample: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Change:
        return cls(
            id=row["id"],
            proposal_id=row["proposal_id"],
            applied_at=row["applied_at"],
            files=json.loads(row["files"] or "[]"),
            insertions=row["insertions"],
            deletions=row["deletions"],
            commit_sha=row["commit_sha"],
            reverted_at=row["reverted_at"],
            is_sample=bool(row["is_sample"]),
        )


@dataclass(frozen=True)
class TestRun:
    id: int
    proposal_id: int | None
    change_id: int | None
    started_at: str
    finished_at: str
    command: str
    exit_code: int | None
    status: str
    output_tail: str
    is_sample: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TestRun:
        return cls(
            id=row["id"],
            proposal_id=row["proposal_id"],
            change_id=row["change_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            command=row["command"],
            exit_code=row["exit_code"],
            status=row["status"],
            output_tail=row["output_tail"] or "",
            is_sample=bool(row["is_sample"]),
        )


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    kind: str
    ref_table: str | None
    ref_id: int | None
    message: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})
