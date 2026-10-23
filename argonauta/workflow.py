"""The core loop: propose -> apply -> test -> commit / revert.

Every function records what it did in the store's events table so the history
is complete even when a step fails halfway.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from argonauta import github, gitops
from argonauta.config import Settings
from argonauta.db import Store, StoreError
from argonauta.diffs import FORBIDDEN_PREFIXES, ValidationResult, validate_diff
from argonauta.indexer import search
from argonauta.logging_setup import get_logger
from argonauta.models import REVIEW_MODES, REVIEW_SOURCES, Change, Proposal, Review, TestRun
from argonauta.prompts import (
    ContextFile,
    ProposalRequest,
    ReviewComment,
    ReviewRequest,
    parse_reply,
    parse_review_reply,
)
from argonauta.providers import ProviderError, ProviderUnavailable, get_provider
from argonauta.runner import RunnerError, run_tests

MAX_TASK_CHARS = 4000
TEST_COMMAND_KEY = "test_command"
LENIENT_NOTE = "applied with reduced context matching (git apply -C1): the diff's line numbers or context did not match exactly; review it"
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
        from argonauta.runner import check_command

        try:
            check_command(test_command)
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
    mentioned: list[str] = field(default_factory=list)  # files named in the task, sent first

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]


_PATHISH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\-]*")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def files_mentioned(task: str, paths: list[str]) -> list[str]:
    """Indexed files the task names, tolerating sloppy spellings like READ.ME."""
    by_path = {p.lower(): p for p in paths}
    by_base: dict[str, list[str]] = {}
    by_stem: dict[str, list[str]] = {}
    for p in paths:
        base = p.rsplit("/", 1)[-1].lower()
        by_base.setdefault(base, []).append(p)
        by_stem.setdefault(_squash(base.split(".")[0]), []).append(p)
    found: list[str] = []
    for token in _PATHISH_RE.findall(task):
        tok = token.strip("./").lower()
        if len(tok) < 3:
            continue
        candidates = by_path.get(tok) and [by_path[tok]] or by_base.get(tok) or by_stem.get(_squash(tok)) or []
        if not candidates:
            continue
        # Several files share the name (README.md at the root and in examples/):
        # the shallowest one is what a person means; ties keep all of them.
        depth = min(c.count("/") for c in candidates)
        for c in sorted(c for c in candidates if c.count("/") == depth):
            if c not in found:
                found.append(c)
    return found


def select_context(settings: Settings, store: Store, task: str) -> ContextSelection:
    selection = ContextSelection()
    budget = settings.max_context_bytes
    all_paths = [f.path for f in store.list_files()]
    selection.mentioned = files_mentioned(task, all_paths)
    hits = search(store, task, limit=settings.max_context_files * 2)
    ordered = selection.mentioned + [h.file.path for h in hits if h.file.path not in selection.mentioned]
    for path in ordered:
        if len(selection.files) >= settings.max_context_files:
            break
        abs_path = settings.repo_root / path
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            selection.skipped.append(path)
            continue
        size = len(content.encode("utf-8", "replace"))
        if size > budget:
            selection.skipped.append(path)
            continue
        budget -= size
        selection.files.append(ContextFile(path=path, content=content))
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
    if not parsed.diff.strip():
        status = "invalid"
        notes = "the reply contains no diff (the model answered in prose); nothing to apply, read its explanation"
    elif not validation.ok:
        status = "invalid"
        notes = "; ".join(validation.errors)
    else:
        mode, apply_problem = gitops.apply_mode(settings.repo_root, parsed.diff)
        if mode is None:
            status = "invalid"
            notes = f"does not apply cleanly: {apply_problem}"
        elif mode == "lenient":
            notes = LENIENT_NOTE
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


# ----- review -----------------------------------------------------------------
DIFF_CHAR_LIMIT = 300_000
COMMENT_LIMIT = 200
DRAFT_CHAR_LIMIT = 20_000
ALL_FILES_OMITTED_NOTE = "every file was omitted because the diff exceeds the limit"
HEADERLESS_CUT_NOTE = "diff had no file headers and was cut to the limit"
EMPTY_SOURCE_ERROR = "Nothing to review: the source diff is empty"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_HEADER_PATHS_RE = re.compile(r"^a/(.*) b/(.*)$")
_FILE_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)


@dataclass
class ReviewSpec:
    mode: str
    source_kind: str
    source_ref: str = ""
    draft: str = ""


@dataclass
class ReviewOutcome:
    review: Review
    request: ReviewRequest


@dataclass
class _Source:
    label: str
    diff: str
    comments: list[ReviewComment] = field(default_factory=list)
    pr_title: str = ""
    pr_body: str = ""
    source_ref: str = ""


def _chunk_path(header: str) -> str:
    """The new-side path of a `diff --git` header line (same rule as patch.js)."""
    rest = header.rstrip("\r\n")[len("diff --git "):]
    m = _HEADER_PATHS_RE.match(rest)
    if m:
        return m.group(2)
    if rest.endswith('"') and ' "' in rest:  # quoted path: diff --git "a/x y" "b/x y"
        quoted = rest[rest.rindex(' "') + 2:-1]
        return quoted[2:] if quoted.startswith("b/") else quoted
    return rest


_FILE_HEADER_SPLIT = re.compile(r"(?m)^(?=diff --git )")


def truncate_diff(diff: str, limit: int = DIFF_CHAR_LIMIT, notes: list[str] | None = None) -> tuple[str, list[str]]:
    """Drop whole files, largest first, until the diff fits in `limit` characters.

    The diff is split at column-0 `diff --git ` lines (inside a hunk every line
    starts with "+", "-", " " or "\\", so these are always file boundaries). Text
    before the first header is kept as is. Among files of equal size the later
    one is dropped first. Returns the kept diff and the dropped paths in their
    original order. When the text before the first header (or a diff with no
    header at all) is still over the limit, it is cut at the last newline that
    fits and HEADERLESS_CUT_NOTE is appended to `notes`.
    """
    # split on "\n" only: str.splitlines also breaks at \x0c, \x1c, \u2028 ... inside lines
    parts = _FILE_HEADER_SPLIT.split(diff)
    preamble = "" if parts[0].startswith("diff --git ") else parts.pop(0)
    chunks = [p for p in parts if p]
    total = len(diff)
    dropped: set[int] = set()
    for i in sorted(range(len(chunks)), key=lambda i: (-len(chunks[i]), -i)):
        if total <= limit:
            break
        dropped.add(i)
        total -= len(chunks[i])
    rest = "".join(c for i, c in enumerate(chunks) if i not in dropped)
    if len(preamble) + len(rest) > limit:
        room = max(0, limit - len(rest))
        cut = preamble[:room]
        preamble = cut[: cut.rfind("\n") + 1] if "\n" in cut else cut
        if notes is not None:
            notes.append(HEADERLESS_CUT_NOTE)
    kept = preamble + rest
    return kept, [_chunk_path(chunks[i].split("\n", 1)[0]) for i in sorted(dropped)]


def _excluded_review_path(path: str) -> bool:
    """Same rule as proposals: never send .env* files or anything under .git/ or .argonauta/."""
    if any(path.startswith(pre) for pre in FORBIDDEN_PREFIXES):
        return True
    return path.rsplit("/", 1)[-1].startswith(".env")


def _drop_excluded(diff: str) -> tuple[str, list[str]]:
    """Remove the file chunks whose old or new path is excluded; return the rest and the dropped paths."""
    parts = _FILE_HEADER_SPLIT.split(diff)
    kept: list[str] = []
    dropped: list[str] = []
    for part in parts:
        if part.startswith("diff --git "):
            header = part.split("\n", 1)[0]
            m = _HEADER_PATHS_RE.match(header.rstrip("\r")[len("diff --git "):])
            paths = {_chunk_path(header)} | ({m.group(1), m.group(2)} if m else set())
            if any(_excluded_review_path(p) for p in paths):
                dropped.append(_chunk_path(header))
                continue
        kept.append(part)
    return "".join(kept), dropped


def validate_review_spec(spec: ReviewSpec) -> None:
    if spec.mode not in REVIEW_MODES:
        raise ValidationError(f"Unknown review mode {spec.mode!r}; expected one of {', '.join(sorted(REVIEW_MODES))}.")
    if spec.source_kind not in REVIEW_SOURCES:
        raise ValidationError(f"Unknown review source {spec.source_kind!r}; expected one of {', '.join(sorted(REVIEW_SOURCES))}.")
    if spec.mode == "received" and spec.source_kind != "pr":
        raise ValidationError("Reviewing received comments needs a pull request as the source.")
    if spec.mode == "mine":
        if not spec.draft.strip():
            raise ValidationError("The draft review is empty; paste the review you want checked.")
        if len(spec.draft) > DRAFT_CHAR_LIMIT:
            raise ValidationError(f"The draft review is longer than {DRAFT_CHAR_LIMIT} characters.")
    ref = spec.source_ref.strip()
    if spec.source_kind == "commit" and not _SHA_RE.match(ref):
        raise ValidationError("The commit must be a SHA of 7 to 40 hexadecimal characters.")
    if spec.source_kind == "pr" and not (ref.isascii() and ref.isdigit() and int(ref) > 0):
        raise ValidationError("The pull request must be given by its number (a positive integer).")


def _source_worktree(settings: Settings) -> _Source:
    return _Source(label="Working tree", diff=gitops.worktree_diff(settings.repo_root))


def _source_commit(settings: Settings, sha: str) -> _Source:
    try:
        detail = gitops.commit_detail(settings.repo_root, sha)
    except gitops.GitError as exc:
        raise WorkflowError(f"Could not read commit {sha}: {exc}") from exc
    c = detail.commit
    diff = detail.diff
    try:
        if len(gitops.commit_parents(settings.repo_root, c.sha)) > 1:
            # `git show` gives a combined `diff --cc` (or nothing) for merges; review against the first parent.
            diff = gitops.commit_diff_first_parent(settings.repo_root, c.sha)
    except gitops.GitError as exc:
        raise WorkflowError(f"Could not read commit {sha}: {exc}") from exc
    return _Source(label=f"commit {c.short} {c.subject}", diff=diff, source_ref=c.sha)


def _source_branch(settings: Settings) -> _Source:
    root = settings.repo_root
    base = gitops.default_base_branch(root)
    if base is None:
        raise WorkflowError("No main or master branch to compare against")
    branch = gitops.current_branch(root)
    try:
        diff = gitops.range_diff(root, base)
    except gitops.GitError as exc:
        raise WorkflowError(f"Could not compare {branch} with {base}: {exc}") from exc
    return _Source(label=f"{branch} vs {base}", diff=diff, source_ref=base)


def _source_pr(settings: Settings, number: int, with_comments: bool) -> _Source:
    root = settings.repo_root
    try:
        pr = github.pr_detail(root, number)
        diff = github.pr_diff(root, number)
        raw_comments = github.pr_comments(root, number) if with_comments else []
    except github.GitHubError as exc:
        raise WorkflowError(str(exc)) from exc
    comments = [
        ReviewComment(author=c.get("author") or "", body=c.get("body") or "", path=c.get("path"), line=c.get("line"),
                      hunk=c.get("hunk") or "", reply=bool(c.get("reply")), outdated=bool(c.get("outdated")))
        for c in raw_comments
    ]
    title = pr.get("title") or ""
    return _Source(label=f"PR #{number} {title}", diff=diff, comments=comments, pr_title=title, pr_body=pr.get("body") or "", source_ref=str(number))


def _build_source(settings: Settings, spec: ReviewSpec) -> _Source:
    kind, ref = spec.source_kind, spec.source_ref.strip()
    if kind == "worktree":
        return _source_worktree(settings)
    if kind == "commit":
        return _source_commit(settings, ref)
    if kind == "branch":
        return _source_branch(settings)
    return _source_pr(settings, int(ref), with_comments=spec.mode == "received")


def review(
    settings: Settings,
    store: Store,
    spec: ReviewSpec,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> ReviewOutcome:
    validate_review_spec(spec)
    if on_progress:
        on_progress("reading the source")
    source = _build_source(settings, spec)

    source_diff, excluded = _drop_excluded(source.diff)
    if not source_diff.strip() and not source.comments:
        raise WorkflowError(EMPTY_SOURCE_ERROR)
    notes: list[str] = []
    # Read DIFF_CHAR_LIMIT at call time so tests can lower it.
    diff, too_big = truncate_diff(source_diff, DIFF_CHAR_LIMIT, notes)
    omitted = excluded + too_big
    omitted_set = set(omitted)
    comments = [
        # excluded files: drop the hunk too, it quotes the file's content
        replace(c, file_omitted=True, hunk="" if c.path and _excluded_review_path(c.path) else c.hunk) if c.path in omitted_set else c
        for c in source.comments
    ]
    if len(comments) > COMMENT_LIMIT:
        notes.append(f"{len(comments) - COMMENT_LIMIT} later comments were not sent")
        comments = comments[:COMMENT_LIMIT]
    if too_big and not _FILE_HEADER_RE.search(diff):
        notes.append(ALL_FILES_OMITTED_NOTE)

    summary_text = repo_summary(settings, store) if store.file_count() > 0 else f"root: {settings.repo_root.name}"
    request = ReviewRequest(
        mode=spec.mode,
        source_kind=spec.source_kind,
        source_label=source.label,
        diff=diff,
        omitted_files=omitted,
        comments=comments,
        draft=spec.draft if spec.mode == "mine" else "",
        pr_title=source.pr_title,
        pr_body=source.pr_body,
        repo_summary=summary_text,
        notes=notes,
    )
    stored = dict(
        mode=spec.mode,
        source_kind=spec.source_kind,
        source_ref=source.source_ref,
        source_label=source.label,
        source_diff=diff,
        omitted_files=omitted,
        comments=[asdict(c) for c in comments],
        draft=request.draft,
    )

    provider = get_provider(settings)
    if on_progress:
        on_progress(f"asking {provider.name} for a review")
    try:
        reply = provider.review(request, on_progress)
    except ProviderUnavailable:
        raise
    except ProviderError as exc:
        failed = store.create_review(**stored, provider=provider.name, model="", status="failed", summary="", findings=[], raw=str(exc))
        store.record_event("review.failed", f"review {failed.id}: provider {provider.name}: {exc}", "reviews", failed.id)
        log.warning("review failed id=%s provider=%s", failed.id, provider.name)
        raise WorkflowError(str(exc)) from exc

    parsed = parse_review_reply(reply.text)
    findings = list(parsed.findings)  # empty when the reply did not parse
    if notes:
        findings.append({"kind": "finding", "severity": "low", "title": "Not everything was sent", "body": "\n".join(notes)})
    saved = store.create_review(
        **stored,
        provider=provider.name,
        model=reply.model,
        status="ok" if parsed.ok else "unparsed",
        summary=parsed.summary if parsed.ok else "",
        findings=findings,
        raw=reply.text,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )
    n = len(saved.findings)
    disagree = sum(1 for f in saved.findings if f.get("verdict") == "disagree")
    store.record_event("review", f"review {saved.id}: {spec.mode} of {source.label}, {n} findings, {disagree} disagree", "reviews", saved.id)
    log.info("review id=%s status=%s findings=%s provider=%s", saved.id, saved.status, n, provider.name)
    return ReviewOutcome(review=saved, request=request)


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
    mode, problem = gitops.apply_mode(settings.repo_root, proposal.diff)
    if mode is None:
        store.update_proposal_status(proposal.id, "invalid", f"does not apply cleanly: {problem}")
        raise WorkflowError(f"The diff no longer applies cleanly: {problem}")
    if mode == "lenient" and LENIENT_NOTE not in proposal.notes:
        store.update_proposal_status(proposal.id, proposal.status, (proposal.notes + "; " if proposal.notes else "") + LENIENT_NOTE)

    if on_progress:
        on_progress("applying diff")
    gitops.apply_diff(settings.repo_root, proposal.diff, lenient=(mode == "lenient"))
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
        mode, problem = gitops.apply_mode(settings.repo_root, proposal.diff, reverse=True)
        if mode is None:
            raise WorkflowError(f"The diff cannot be reversed cleanly (files changed since apply?): {problem}")
        gitops.apply_diff(settings.repo_root, proposal.diff, reverse=True, lenient=(mode == "lenient"))
    store.mark_change_reverted(change.id)
    store.update_proposal_status(proposal.id, "reverted")
    store.record_event("revert", f"reverted change {change.id}" + (f" with commit {new_sha[:10]}" if new_sha else ""), "changes", change.id)
    log.info("revert change=%s", change.id)
    return store.get_change(change.id), new_sha


__all__ = [
    "ApplyOutcome",
    "ContextSelection",
    "ProposeOutcome",
    "ReviewOutcome",
    "ReviewSpec",
    "StoreError",
    "ValidationError",
    "WorkflowError",
    "apply_proposal",
    "commit_change",
    "init_repo",
    "propose",
    "reject_proposal",
    "revert_proposal",
    "review",
    "run_tests_now",
    "select_context",
    "test_command_for",
    "truncate_diff",
    "validate_review_spec",
    "validate_task",
]
