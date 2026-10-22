"""The core loop: propose -> apply -> test -> commit / revert.

Every function records what it did in the store's events table so the history
is complete even when a step fails halfway.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from argonauta import gitops
from argonauta.config import Settings
from argonauta.db import Store, StoreError
from argonauta.diffs import ValidationResult, validate_diff
from argonauta.indexer import search
from argonauta.logging_setup import get_logger
from argonauta.models import Change, Proposal, TestRun
from argonauta.prompts import ContextFile, ProposalRequest, parse_reply
from argonauta.providers import ProviderError, ProviderUnavailable, get_provider
from argonauta.runner import RunnerError, run_tests

MAX_TASK_CHARS = 4000
TEST_COMMAND_KEY = "test_command"
log = get_logger()


class WorkflowError(RuntimeError):
    """A step could not proceed; the message is safe to show the user."""


class ValidationError(WorkflowError):
    """User input was rejected before anything happened."""


def validate_task(task: str) -> str:
    task = (task or "").strip()
    if not task:
        raise ValidationError("The task description is empty.")
    if len(task) > MAX_TASK_CHARS:
        raise ValidationError(f"The task description is longer than {MAX_TASK_CHARS} characters.")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in task):
        raise ValidationError("The task description contains control characters.")
    return task


def test_command_for(settings: Settings, store: Store) -> str | None:
    return settings.test_command_override or store.get_setting(TEST_COMMAND_KEY)


# ----- init -------------------------------------------------------------------
def init_repo(settings: Settings, test_command: str | None) -> Store:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store.open(settings.db_path, create=True)
    if store.get_meta("repo_root") is None:
        store.set_meta("repo_root", str(settings.repo_root))
    if test_command is not None:
        from argonauta.runner import parse_command

        try:
            parse_command(test_command)
        except RunnerError as exc:
            raise ValidationError(str(exc)) from exc
        store.set_setting(TEST_COMMAND_KEY, test_command)
    gitops.ensure_excluded(settings.repo_root, "/.argonauta/")
    store.record_event("init", "store initialised")
    log.info("init store=%s", settings.db_path)
    return store


# ----- propose ----------------------------------------------------------------
@dataclass
class ContextSelection:
    files: list[ContextFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def select_context(settings: Settings, store: Store, task: str) -> ContextSelection:
    selection = ContextSelection()
    hits = search(store, task, limit=settings.max_context_files * 2)
    budget = settings.max_context_bytes
    for hit in hits:
        if len(selection.files) >= settings.max_context_files:
            break
        abs_path = settings.repo_root / hit.file.path
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            selection.skipped.append(hit.file.path)
            continue
        size = len(content.encode("utf-8", "replace"))
        if size > budget:
            selection.skipped.append(hit.file.path)
            continue
        budget -= size
        selection.files.append(ContextFile(path=hit.file.path, content=content))
    return selection


def repo_summary(settings: Settings, store: Store) -> str:
    branch = gitops.current_branch(settings.repo_root)
    count = store.file_count()
    languages: dict[str, int] = {}
    for f in store.list_files():
        if f.language:
            languages[f.language] = languages.get(f.language, 0) + 1
    top = ", ".join(f"{lang} ({n})" for lang, n in sorted(languages.items(), key=lambda kv: -kv[1])[:5])
    return f"root: {settings.repo_root.name}\nbranch: {branch}\nindexed files: {count}\nlanguages: {top or 'unknown'}"


@dataclass
class ProposeOutcome:
    proposal: Proposal | None
    context: ContextSelection
    validation: ValidationResult | None
    apply_problem: str | None = None


def propose(
    settings: Settings,
    store: Store,
    task: str,
    *,
    dry_run: bool = False,
    from_diff: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ProposeOutcome:
    task = validate_task(task)
    if store.file_count() == 0 and from_diff is None:
        raise WorkflowError("Nothing is indexed yet. Run `argonauta index` first.")
    context = select_context(settings, store, task) if from_diff is None else ContextSelection()
    if dry_run:
        return ProposeOutcome(proposal=None, context=context, validation=None)

    if from_diff is not None:
        try:
            raw = from_diff.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ValidationError(f"Could not read {from_diff}: {exc.strerror}") from exc
        provider_name, model = "manual", "manual"
        input_tokens = output_tokens = None
    else:
        provider = get_provider(settings)
        try:
            reply = provider.propose(ProposalRequest(task=task, repo_summary=repo_summary(settings, store), files=context.files), on_progress)
        except ProviderUnavailable:
            raise
        except ProviderError as exc:
            store.record_event("propose.failed", f"provider {provider.name}: {exc}")
            log.warning("propose failed provider=%s", provider.name)
            raise WorkflowError(str(exc)) from exc
        raw, provider_name, model = reply.text, provider.name, reply.model
        input_tokens, output_tokens = reply.input_tokens, reply.output_tokens

    parsed = parse_reply(raw)
    validation = validate_diff(parsed.diff, settings.repo_root)
    apply_problem = None
    status = "proposed"
    notes = ""
    if not validation.ok:
        status = "invalid"
        notes = "; ".join(validation.errors)
    else:
        apply_problem = gitops.apply_check(settings.repo_root, parsed.diff)
        if apply_problem:
            status = "invalid"
            notes = f"does not apply cleanly: {apply_problem}"
    proposal = store.create_proposal(
        task=task,
        provider=provider_name,
        model=model,
        status=status,
        explanation=parsed.explanation,
        diff=parsed.diff,
        context_files=context.paths,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        notes=notes,
    )
    store.record_event("propose", f"proposal {proposal.id} created with status {status} ({len(validation.files)} files)", "proposals", proposal.id)
    log.info("propose id=%s status=%s files=%s provider=%s", proposal.id, status, len(validation.files), provider_name)
    return ProposeOutcome(proposal=proposal, context=context, validation=validation, apply_problem=apply_problem)


# ----- apply / test / commit --------------------------------------------------
@dataclass
class ApplyOutcome:
    proposal: Proposal
    change: Change
    test_run: TestRun | None
    commit_sha: str | None
    skipped_commit_reason: str | None = None


def _dirty_paths(repo_root: Path, paths: list[str]) -> list[str]:
    return [e.path for e in gitops.status_entries(repo_root, paths)]


def apply_proposal(
    settings: Settings,
    store: Store,
    proposal_id: int,
    *,
    run_tests_after: bool = True,
    commit_after: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> ApplyOutcome:
    proposal = store.get_proposal(proposal_id)
    if proposal.is_sample:
        raise WorkflowError("Sample proposals cannot be applied. Run `argonauta sample clear` to remove them.")
    if proposal.status != "proposed":
        raise WorkflowError(f"Proposal {proposal.id} has status {proposal.status!r}; only 'proposed' can be applied.")
    validation = validate_diff(proposal.diff, settings.repo_root)
    if not validation.ok:
        store.update_proposal_status(proposal.id, "invalid", "; ".join(validation.errors))
        raise WorkflowError("The stored diff is no longer valid: " + "; ".join(validation.errors))
    dirty = _dirty_paths(settings.repo_root, [p for p in validation.paths if (settings.repo_root / p).exists()])
    if dirty:
        raise WorkflowError("These files have uncommitted changes; commit or stash them first: " + ", ".join(dirty))
    problem = gitops.apply_check(settings.repo_root, proposal.diff)
    if problem:
        store.update_proposal_status(proposal.id, "invalid", f"does not apply cleanly: {problem}")
        raise WorkflowError(f"The diff no longer applies cleanly: {problem}")

    if on_progress:
        on_progress("applying diff")
    gitops.apply_diff(settings.repo_root, proposal.diff)
    change = store.create_change(
        proposal_id=proposal.id,
        files=validation.paths,
        insertions=validation.insertions,
        deletions=validation.deletions,
    )
    store.update_proposal_status(proposal.id, "applied")
    store.record_event("apply", f"applied proposal {proposal.id} to {len(validation.paths)} files", "changes", change.id)
    log.info("apply proposal=%s change=%s files=%s", proposal.id, change.id, len(validation.paths))

    test_run = None
    if run_tests_after:
        command = test_command_for(settings, store)
        if command:
            if on_progress:
                on_progress(f"running tests: {command}")
            test_run = _record_test_run(settings, store, command, proposal.id, change.id)

    commit_sha = None
    skipped_reason = None
    if commit_after:
        if test_run is not None and test_run.status != "passed":
            skipped_reason = f"tests {test_run.status}; nothing was committed"
        else:
            if on_progress:
                on_progress("committing")
            commit_sha = _commit_change(settings, store, proposal, change, validation.paths)
    return ApplyOutcome(proposal=store.get_proposal(proposal.id), change=store.get_change(change.id), test_run=test_run, commit_sha=commit_sha, skipped_commit_reason=skipped_reason)


def _record_test_run(settings: Settings, store: Store, command: str, proposal_id: int | None, change_id: int | None) -> TestRun:
    try:
        result = run_tests(settings.repo_root, command, timeout=settings.test_timeout)
    except RunnerError as exc:
        raise ValidationError(str(exc)) from exc
    run = store.create_test_run(
        proposal_id=proposal_id,
        change_id=change_id,
        started_at=result.started_at,
        finished_at=result.finished_at,
        command=command,
        exit_code=result.exit_code,
        status=result.status,
        output_tail=result.output_tail,
    )
    store.record_event("test", f"tests {run.status} (exit {run.exit_code}) in {result.duration_s:.1f}s", "test_runs", run.id)
    log.info("test run=%s status=%s exit=%s", run.id, run.status, run.exit_code)
    return run


def run_tests_now(settings: Settings, store: Store, proposal_id: int | None = None) -> TestRun:
    command = test_command_for(settings, store)
    if not command:
        raise WorkflowError("No test command is configured. Run `argonauta config set-test-command \"<command>\"`.")
    change_id = None
    if proposal_id is not None:
        change = store.change_for_proposal(proposal_id)
        change_id = change.id if change else None
    return _record_test_run(settings, store, command, proposal_id, change_id)


def _commit_change(settings: Settings, store: Store, proposal: Proposal, change: Change, paths: list[str]) -> str:
    gitops.stage_paths(settings.repo_root, paths)
    first_line = proposal.task.strip().splitlines()[0][:72]
    message = f"{first_line}\n\nArgonauta proposal #{proposal.id} ({proposal.provider}/{proposal.model})"
    try:
        sha = gitops.commit(settings.repo_root, message)
    except gitops.GitError as exc:
        store.record_event("commit.failed", str(exc), "changes", change.id)
        raise WorkflowError(f"Commit failed: {exc}") from exc
    store.set_change_commit(change.id, sha)
    store.record_event("commit", f"committed change {change.id} as {sha[:10]}", "changes", change.id)
    log.info("commit change=%s sha=%s", change.id, sha[:10])
    return sha


def commit_change(settings: Settings, store: Store, proposal_id: int) -> str:
    proposal = store.get_proposal(proposal_id)
    change = store.change_for_proposal(proposal_id)
    if change is None or proposal.status != "applied":
        raise WorkflowError(f"Proposal {proposal_id} is not applied, so there is nothing to commit.")
    if change.commit_sha:
        raise WorkflowError(f"Change {change.id} is already committed as {change.commit_sha[:10]}.")
    return _commit_change(settings, store, proposal, change, change.files)


# ----- reject / revert --------------------------------------------------------
def reject_proposal(store: Store, proposal_id: int, reason: str = "") -> Proposal:
    proposal = store.get_proposal(proposal_id)
    if proposal.status not in ("proposed", "invalid"):
        raise WorkflowError(f"Proposal {proposal.id} has status {proposal.status!r}; only pending proposals can be rejected.")
    updated = store.update_proposal_status(proposal.id, "rejected", reason or proposal.notes)
    store.record_event("reject", f"rejected proposal {proposal.id}" + (f": {reason}" if reason else ""), "proposals", proposal.id)
    return updated


def revert_proposal(settings: Settings, store: Store, proposal_id: int) -> tuple[Change, str | None]:
    proposal = store.get_proposal(proposal_id)
    change = store.change_for_proposal(proposal_id)
    if change is None or proposal.status != "applied":
        raise WorkflowError(f"Proposal {proposal_id} is not applied, so there is nothing to revert.")
    if change.reverted_at:
        raise WorkflowError(f"Change {change.id} was already reverted at {change.reverted_at}.")
    new_sha = None
    if change.commit_sha:
        if not gitops.commit_exists(settings.repo_root, change.commit_sha):
            raise WorkflowError(f"Commit {change.commit_sha[:10]} no longer exists in this repository.")
        dirty = _dirty_paths(settings.repo_root, change.files)
        if dirty:
            raise WorkflowError("These files have uncommitted changes; commit or stash them first: " + ", ".join(dirty))
        try:
            new_sha = gitops.revert_commit(settings.repo_root, change.commit_sha)
        except gitops.GitError as exc:
            raise WorkflowError(f"git revert failed: {exc}") from exc
    else:
        problem = gitops.apply_check(settings.repo_root, proposal.diff, reverse=True)
        if problem:
            raise WorkflowError(f"The diff cannot be reversed cleanly (files changed since apply?): {problem}")
        gitops.apply_diff(settings.repo_root, proposal.diff, reverse=True)
    store.mark_change_reverted(change.id)
    store.update_proposal_status(proposal.id, "reverted")
    store.record_event("revert", f"reverted change {change.id}" + (f" with commit {new_sha[:10]}" if new_sha else ""), "changes", change.id)
    log.info("revert change=%s", change.id)
    return store.get_change(change.id), new_sha


__all__ = [
    "ApplyOutcome",
    "ContextSelection",
    "ProposeOutcome",
    "StoreError",
    "ValidationError",
    "WorkflowError",
    "apply_proposal",
    "commit_change",
    "init_repo",
    "propose",
    "reject_proposal",
    "revert_proposal",
    "run_tests_now",
    "select_context",
    "test_command_for",
    "validate_task",
]
