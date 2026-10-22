"""JSON export and import so the user is never trapped in the app.

The export holds every record the user created (proposals, changes, test
runs, events, settings) plus index metadata (paths and hashes, never file
contents). Diffs are included because they are the user's own work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argonauta import __version__
from argonauta.db import Store
from argonauta.models import PROPOSAL_STATUSES, TEST_STATUSES, utcnow

FORMAT = "argonauta-export"
FORMAT_VERSION = 1
MAX_IMPORT_BYTES = 200_000_000


class ExchangeError(ValueError):
    pass


def export_data(store: Store) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "app_version": __version__,
        "exported_at": utcnow(),
        "repo_root": store.get_meta("repo_root"),
        "settings": store.all_settings(),
        "files": [{k: r[k] for k in ("path", "size", "sha256", "language", "line_count", "indexed_at")} for r in store.raw_rows("files")],
        "proposals": store.raw_rows("proposals"),
        "changes": store.raw_rows("changes"),
        "test_runs": store.raw_rows("test_runs"),
        "events": store.raw_rows("events"),
    }


def export_to_file(store: Store, path: Path) -> dict[str, int]:
    data = export_data(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {k: len(data[k]) for k in ("proposals", "changes", "test_runs", "events", "files")}


# ----- import -------------------------------------------------------------------
def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ExchangeError(message)


def _opt_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    _require(isinstance(value, int) and not isinstance(value, bool), f"{field} must be an integer or null")
    return value


def _str(value: Any, field: str, max_len: int = 2_000_000) -> str:
    _require(isinstance(value, str), f"{field} must be a string")
    _require(len(value) <= max_len, f"{field} is too long")
    return value


def _str_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ExchangeError(f"{field} must be a JSON list") from exc
    _require(isinstance(value, list) and all(isinstance(v, str) for v in value), f"{field} must be a list of strings")
    return value


def load_export(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExchangeError(f"Cannot read {path}: {exc.strerror}") from exc
    _require(size <= MAX_IMPORT_BYTES, f"{path} is larger than {MAX_IMPORT_BYTES} bytes")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExchangeError(f"{path} is not valid JSON: {exc}") from exc
    _require(isinstance(data, dict), "export root must be an object")
    _require(data.get("format") == FORMAT, f"not an Argonauta export (format={data.get('format')!r})")
    _require(data.get("version") == FORMAT_VERSION, f"unsupported export version {data.get('version')!r}")
    for key in ("proposals", "changes", "test_runs", "events"):
        _require(isinstance(data.get(key, []), list), f"{key} must be a list")
    _require(isinstance(data.get("settings", {}), dict), "settings must be an object")
    return data


@dataclass
class ImportReport:
    proposals: int = 0
    changes: int = 0
    test_runs: int = 0
    events: int = 0
    skipped_settings: dict[str, str] | None = None


def import_data(store: Store, data: dict[str, Any], *, replace: bool = False) -> ImportReport:
    """Insert records with fresh ids (merge) or after wiping records (replace)."""
    report = ImportReport()
    if replace:
        store.clear_records()
    proposal_ids: dict[int, int] = {}
    change_ids: dict[int, int] = {}

    for raw in data.get("proposals", []):
        _require(isinstance(raw, dict), "each proposal must be an object")
        status = _str(raw.get("status"), "proposal.status", 32)
        _require(status in PROPOSAL_STATUSES, f"unknown proposal status {status!r}")
        old_id = _opt_int(raw.get("id"), "proposal.id")
        p = store.create_proposal(
            task=_str(raw.get("task", ""), "proposal.task", 10_000),
            provider=_str(raw.get("provider", "unknown"), "proposal.provider", 64),
            model=_str(raw.get("model", "unknown"), "proposal.model", 128),
            status=status,
            explanation=_str(raw.get("explanation", ""), "proposal.explanation"),
            diff=_str(raw.get("diff", ""), "proposal.diff"),
            context_files=_str_list(raw.get("context_files", []), "proposal.context_files"),
            input_tokens=_opt_int(raw.get("input_tokens"), "proposal.input_tokens"),
            output_tokens=_opt_int(raw.get("output_tokens"), "proposal.output_tokens"),
            notes=_str(raw.get("notes", ""), "proposal.notes", 10_000),
            is_sample=bool(raw.get("is_sample", 0)),
            created_at=_str(raw.get("created_at", utcnow()), "proposal.created_at", 64),
        )
        if old_id is not None:
            proposal_ids[old_id] = p.id
        report.proposals += 1

    for raw in data.get("changes", []):
        _require(isinstance(raw, dict), "each change must be an object")
        old_pid = _opt_int(raw.get("proposal_id"), "change.proposal_id")
        _require(old_pid in proposal_ids, f"change references unknown proposal {old_pid}")
        old_id = _opt_int(raw.get("id"), "change.id")
        c = store.create_change(
            proposal_id=proposal_ids[old_pid],
            files=_str_list(raw.get("files", []), "change.files"),
            insertions=_opt_int(raw.get("insertions", 0), "change.insertions") or 0,
            deletions=_opt_int(raw.get("deletions", 0), "change.deletions") or 0,
            commit_sha=raw.get("commit_sha") if isinstance(raw.get("commit_sha"), str) else None,
            is_sample=bool(raw.get("is_sample", 0)),
            applied_at=_str(raw.get("applied_at", utcnow()), "change.applied_at", 64),
        )
        if raw.get("reverted_at"):
            store.mark_change_reverted(c.id)
        if old_id is not None:
            change_ids[old_id] = c.id
        report.changes += 1

    for raw in data.get("test_runs", []):
        _require(isinstance(raw, dict), "each test run must be an object")
        status = _str(raw.get("status"), "test_run.status", 32)
        _require(status in TEST_STATUSES, f"unknown test status {status!r}")
        old_pid = _opt_int(raw.get("proposal_id"), "test_run.proposal_id")
        old_cid = _opt_int(raw.get("change_id"), "test_run.change_id")
        store.create_test_run(
            proposal_id=proposal_ids.get(old_pid) if old_pid is not None else None,
            change_id=change_ids.get(old_cid) if old_cid is not None else None,
            started_at=_str(raw.get("started_at", utcnow()), "test_run.started_at", 64),
            finished_at=_str(raw.get("finished_at", utcnow()), "test_run.finished_at", 64),
            command=_str(raw.get("command", ""), "test_run.command", 1000),
            exit_code=_opt_int(raw.get("exit_code"), "test_run.exit_code"),
            status=status,
            output_tail=_str(raw.get("output_tail", ""), "test_run.output_tail", 100_000),
            is_sample=bool(raw.get("is_sample", 0)),
        )
        report.test_runs += 1

    for raw in data.get("events", []):
        _require(isinstance(raw, dict), "each event must be an object")
        ref_table = raw.get("ref_table")
        ref_id = _opt_int(raw.get("ref_id"), "event.ref_id")
        if ref_table == "proposals" and ref_id is not None:
            ref_id = proposal_ids.get(ref_id)
        elif ref_table == "changes" and ref_id is not None:
            ref_id = change_ids.get(ref_id)
        elif ref_table == "test_runs":
            ref_id = None
        store.record_event(
            _str(raw.get("kind", "imported"), "event.kind", 64),
            _str(raw.get("message", ""), "event.message", 10_000),
            ref_table if isinstance(ref_table, str) else None,
            ref_id,
        )
        report.events += 1

    # Settings are never imported: `test_command` is executed by `apply`, so a
    # crafted export must not be able to plant one. They are reported instead
    # so the user can set them on purpose with `argonauta config`.
    report.skipped_settings = {
        k: v for k, v in data.get("settings", {}).items()
        if isinstance(k, str) and isinstance(v, str) and len(k) < 64 and len(v) < 2000
    }
    store.record_event("import", f"imported {report.proposals} proposals, {report.changes} changes, {report.test_runs} test runs")
    return report
