"""Typer command-line interface. Exit codes: 0 ok, 1 failure, 2 invalid input."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from argonauta import __version__, gitops
from argonauta import ui as console_ui
from argonauta.config import ConfigError, Settings, load_settings
from argonauta.db import Store, StoreError
from argonauta.logging_setup import configure_logging, get_logger
from argonauta.providers import ProviderUnavailable, describe_provider
from argonauta.workflow import ValidationError, WorkflowError

app = typer.Typer(
    name="argonauta",
    help="Repository-local Git client assistant: index one codebase, ask one model for a diff, run the tests, record every change.",
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
sample_app = typer.Typer(help="Load or remove clearly labelled sample records.", no_args_is_help=True)
config_app = typer.Typer(help="Show or change per-repository settings.", no_args_is_help=True)
app.add_typer(sample_app, name="sample")
app.add_typer(config_app, name="config")
workspace_app = typer.Typer(help="Manage workspaces (groups of repositories) for the web UI.", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")

EXIT_OK, EXIT_FAIL, EXIT_INVALID = 0, 1, 2


class State:
    settings: Settings | None = None
    repo_override: Path | None = None


state = State()


def _version(value: bool) -> None:
    if value:
        typer.echo(f"argonauta {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    repo: Annotated[Optional[Path], typer.Option("--repo", "-C", help="Repository path (default: the repo containing the current directory).", show_default=False)] = None,
    plain: Annotated[bool, typer.Option("--plain", help="No colour or spinners (also honours NO_COLOR).")] = False,
    version: Annotated[bool, typer.Option("--version", "-V", callback=_version, is_eager=True, help="Print the version and exit.")] = False,
) -> None:
    console_ui.configure(plain)
    state.repo_override = repo


# ----- helpers ------------------------------------------------------------------
def _settings() -> Settings:
    if state.settings is not None:
        return state.settings
    start = (state.repo_override or Path.cwd()).resolve()
    try:
        root = gitops.repo_toplevel(start)
        state.settings = load_settings(root)
    except gitops.GitError as exc:
        console_ui.failure(str(exc))
        console_ui.info("Argonauta works inside a git repository. Run `git init` first, or pass --repo.")
        raise typer.Exit(EXIT_INVALID)
    except ConfigError as exc:
        console_ui.failure(f"Invalid configuration: {exc}")
        raise typer.Exit(EXIT_INVALID)
    configure_logging(state.settings.log_path if state.settings.data_dir.is_dir() else None)
    return state.settings


def _store(settings: Settings) -> Store:
    try:
        return Store.open(settings.db_path)
    except StoreError as exc:
        console_ui.failure(str(exc))
        raise typer.Exit(EXIT_INVALID)


def _fail(exc: Exception, code: int = EXIT_FAIL) -> None:
    console_ui.failure(str(exc))
    raise typer.Exit(code)


def _run(fn, *args, **kwargs):
    """Run a workflow step, mapping its exceptions to exit codes."""
    try:
        return fn(*args, **kwargs)
    except ValidationError as exc:
        _fail(exc, EXIT_INVALID)
    except (WorkflowError, StoreError, gitops.GitError) as exc:
        _fail(exc, EXIT_FAIL)


def _confirm(question: str, yes: bool) -> bool:
    """Ask on a terminal; refuse to guess when stdin is not interactive."""
    if yes:
        return True
    if not sys.stdin.isatty():
        console_ui.failure("This needs confirmation but stdin is not a terminal. Re-run with --yes.")
        raise typer.Exit(EXIT_INVALID)
    return typer.confirm(question, default=False)


def _status_style(status: str) -> str:
    return {
        "proposed": "bold yellow", "applied": "bold green", "rejected": "dim", "invalid": "bold red",
        "reverted": "magenta", "passed": "bold green", "failed": "bold red", "error": "bold red",
    }.get(status, "")


def _status_text(status: str) -> str:
    symbol = {"proposed": "○", "applied": "✓", "rejected": "–", "invalid": "✗", "reverted": "↩", "passed": "✓", "failed": "✗", "error": "!"}.get(status, "")
    return f"[{_status_style(status)}]{symbol} {status}[/]" if _status_style(status) else f"{symbol} {status}"


# ----- init / index / status ----------------------------------------------------
@app.command()
def init(
    test_command: Annotated[Optional[str], typer.Option("--test-command", "-t", help='Command that runs the tests, e.g. "python -m pytest -q".', show_default=False)] = None,
) -> None:
    """Create the private .argonauta/ store inside this repository."""
    from argonauta.workflow import init_repo

    settings = _settings()
    existed = settings.db_path.is_file()
    store = _run(init_repo, settings, test_command)
    configure_logging(settings.log_path)
    store.close()
    console_ui.success(("Store already existed at " if existed else "Store created at ") + str(settings.data_dir))
    if test_command:
        console_ui.info(f"Test command saved: {test_command}")
    else:
        console_ui.warning('No test command set. Add one with `argonauta config set-test-command "python -m pytest -q"`.')
    console_ui.info("Next: `argonauta index`")


@app.command()
def index() -> None:
    """Scan tracked files and (re)build the lexical index."""
    from argonauta.indexer import index_repo

    settings = _settings()
    store = _store(settings)
    con = console_ui.console()
    try:
        if con.is_terminal:
            from rich.progress import BarColumn, Progress, TextColumn

            with Progress(TextColumn("indexing"), BarColumn(), TextColumn("{task.completed}/{task.total}"), console=con, transient=True) as progress:
                task_id = progress.add_task("index", total=None)

                def tick(n: int, total: int) -> None:
                    progress.update(task_id, completed=n, total=total)

                stats = index_repo(store, settings.repo_root, tick)
        else:
            con.print("… indexing")
            stats = index_repo(store, settings.repo_root)
    except gitops.GitError as exc:
        _fail(exc)
    store.record_event("index", f"indexed {stats.indexed}, unchanged {stats.unchanged}, removed {stats.removed}, skipped {stats.skipped}")
    get_logger().info("index indexed=%s unchanged=%s removed=%s skipped=%s", stats.indexed, stats.unchanged, stats.removed, stats.skipped)
    total = store.file_count()
    store.close()
    if total == 0:
        console_ui.empty_state("no indexable files", "The repository has no tracked text files. Add some files, then run `argonauta index` again.")
        return
    console_ui.success(f"Index ready: {total} files ({stats.indexed} indexed, {stats.unchanged} unchanged, {stats.removed} removed, {stats.skipped} skipped)")
    console_ui.info('Next: `argonauta propose "<what you want changed>"`')


@app.command()
def status() -> None:
    """Show repository, index, provider and recent proposals."""
    from argonauta.workflow import test_command_for

    settings = _settings()
    root = settings.repo_root
    branch = gitops.current_branch(root)
    head = gitops.head_sha(root) or "none"
    dirty = gitops.status_entries(root)
    ab = gitops.ahead_behind(root)
    t = console_ui.table("Repository", ["field", "value"])
    t.add_row("path", str(root))
    t.add_row("branch", branch)
    t.add_row("HEAD", head)
    t.add_row("working tree", "clean" if not dirty else f"{len(dirty)} changed/untracked")
    if ab:
        t.add_row("upstream", f"{ab[0]} ahead, {ab[1]} behind")
    console_ui.print_table(t)

    if not settings.db_path.is_file():
        console_ui.empty_state("not initialised", "Run `argonauta init` to create the private store for this repository.")
        return
    store = _store(settings)
    files = store.file_count()
    counts = store.count_proposals_by_status()
    t = console_ui.table("Argonauta", ["field", "value"])
    t.add_row("data", str(settings.data_dir))
    t.add_row("index", f"{files} files, last indexed {store.last_indexed_at()}" if files else "empty (run `argonauta index`)")
    t.add_row("provider", describe_provider(settings))
    t.add_row("test command", test_command_for(settings, store) or "not set")
    t.add_row("proposals", ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none")
    console_ui.print_table(t)
    if settings.effective_provider == "manual":
        console_ui.warning("Degraded mode: without ANTHROPIC_API_KEY, `propose` writes a prompt bundle for you to run elsewhere and `--from-diff` ingests the reply.")
    recent = store.list_proposals(limit=3)
    store.close()
    if recent:
        t = console_ui.table("Recent proposals", ["id", "status", "task"])
        for p in recent:
            t.add_row(str(p.id), _status_text(p.status), console_ui.short(p.task, 60))
        console_ui.print_table(t)


# ----- the core loop -------------------------------------------------------------
@app.command()
def propose(
    task: Annotated[str, typer.Argument(help="What you want changed, in plain words.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show which files would be sent and stop; nothing leaves the machine.")] = False,
    from_diff: Annotated[Optional[Path], typer.Option("--from-diff", help="Skip the model and ingest a diff (or a reply containing one) from this file.", show_default=False)] = None,
) -> None:
    """Ask the model for a diff that does TASK, validate it and record it."""
    from argonauta.workflow import propose as do_propose

    settings = _settings()
    store = _store(settings)
    if from_diff is not None and not from_diff.is_file():
        console_ui.failure(f"--from-diff: {from_diff} is not a file.")
        raise typer.Exit(EXIT_INVALID)
    try:
        if dry_run:
            outcome = _run(do_propose, settings, store, task, dry_run=True)
        else:
            con = console_ui.console()
            if con.is_terminal and from_diff is None:
                with con.status("preparing…", spinner="dots") as st:
                    outcome = _run(do_propose, settings, store, task, from_diff=from_diff, on_progress=lambda m: st.update(f"{m}…"))
            else:
                outcome = _run(do_propose, settings, store, task, from_diff=from_diff, on_progress=lambda m: con.print(f"… {m}"))
    except ProviderUnavailable as exc:
        console_ui.warning("Degraded mode")
        console_ui.panel(str(exc), "Manual provider", style="yellow")
        store.close()
        raise typer.Exit(EXIT_OK)
    ctx = outcome.context
    if dry_run:
        if not ctx.files:
            console_ui.empty_state("no matching files", "No indexed file matched the task words. Try different wording, or run `argonauta index` after adding files.")
        else:
            t = console_ui.table("Context that would be sent", ["path", "bytes"])
            for f in ctx.files:
                t.add_row(f.path, str(f.size))
            console_ui.print_table(t)
            if ctx.skipped:
                console_ui.warning(f"Skipped (over the byte budget): {', '.join(ctx.skipped)}")
        console_ui.info(f"Provider: {describe_provider(settings)}")
        store.close()
        return
    p = outcome.proposal
    assert p is not None and outcome.validation is not None
    store.close()
    if p.status == "invalid":
        console_ui.failure(f"Proposal {p.id} recorded as invalid: {p.notes}")
        if p.explanation:
            console_ui.panel(p.explanation, "Model explanation", style="red")
        console_ui.info("Review it with `argonauta show {0}` or discard it with `argonauta reject {0}`.".format(p.id))
        raise typer.Exit(EXIT_FAIL)
    v = outcome.validation
    console_ui.success(f"Proposal {p.id} ready: {len(v.files)} file(s), +{v.insertions} −{v.deletions}")
    if p.explanation:
        console_ui.panel(p.explanation, "Explanation")
    console_ui.diff_view(p.diff)
    console_ui.info(f"Next: `argonauta apply {p.id}` to apply, test and commit, or `argonauta reject {p.id}`.")


@app.command()
def show(proposal_id: Annotated[int, typer.Argument(help="Proposal id from `argonauta history`.")]) -> None:
    """Show one proposal: metadata, explanation, diff and test results."""
    settings = _settings()
    store = _store(settings)
    try:
        p = store.get_proposal(proposal_id)
    except StoreError as exc:
        store.close()
        _fail(exc, EXIT_INVALID)
    change = store.change_for_proposal(p.id)
    runs = store.list_test_runs(p.id, limit=5)
    store.close()
    t = console_ui.table(f"Proposal {p.id}" + (" [SAMPLE]" if p.is_sample else ""), ["field", "value"])
    t.add_row("status", _status_text(p.status))
    t.add_row("created", p.created_at)
    t.add_row("task", p.task)
    t.add_row("provider", f"{p.provider} / {p.model}")
    t.add_row("context", ", ".join(p.context_files) or "(none)")
    if p.input_tokens is not None or p.output_tokens is not None:
        t.add_row("tokens", f"in {p.input_tokens}, out {p.output_tokens}")
    if p.notes:
        t.add_row("notes", p.notes)
    if change:
        t.add_row("change", f"#{change.id}: {len(change.files)} files, +{change.insertions} −{change.deletions}" + (f", commit {change.commit_sha[:10]}" if change.commit_sha else ", not committed") + (f", reverted {change.reverted_at}" if change.reverted_at else ""))
    console_ui.print_table(t)
    if p.explanation:
        console_ui.panel(p.explanation, "Explanation")
    console_ui.diff_view(p.diff)
    if runs:
        t = console_ui.table("Test runs", ["id", "status", "exit", "finished", "command"])
        for r in runs:
            t.add_row(str(r.id), _status_text(r.status), str(r.exit_code), r.finished_at, r.command)
        console_ui.print_table(t)
        if runs[0].status != "passed" and runs[0].output_tail:
            console_ui.panel(runs[0].output_tail, "Last test output", style="red")


@app.command()
def apply(
    proposal_id: Annotated[int, typer.Argument(help="Proposal id to apply.")],
    run_tests: Annotated[bool, typer.Option("--test/--no-test", help="Run the configured test command after applying.")] = True,
    commit: Annotated[bool, typer.Option("--commit/--no-commit", help="Commit when the tests pass (or when tests are skipped).")] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Apply a proposal's diff, run the tests, and commit on green."""
    from argonauta.workflow import apply_proposal

    settings = _settings()
    store = _store(settings)
    try:
        p = store.get_proposal(proposal_id)
    except StoreError as exc:
        store.close()
        _fail(exc, EXIT_INVALID)
    if not yes:
        console_ui.diff_view(p.diff)
    if not _confirm(f"Apply proposal {p.id} ({console_ui.short(p.task, 50)})?", yes):
        console_ui.info("Nothing changed.")
        store.close()
        raise typer.Exit(EXIT_OK)
    con = console_ui.console()
    if con.is_terminal:
        with con.status("applying…", spinner="dots") as st:
            outcome = _run(apply_proposal, settings, store, p.id, run_tests_after=run_tests, commit_after=commit, on_progress=lambda m: st.update(f"{m}…"))
    else:
        outcome = _run(apply_proposal, settings, store, p.id, run_tests_after=run_tests, commit_after=commit, on_progress=lambda m: con.print(f"… {m}"))
    store.close()
    ch = outcome.change
    console_ui.success(f"Applied proposal {p.id}: {len(ch.files)} file(s), +{ch.insertions} −{ch.deletions}")
    if run_tests and outcome.test_run is None:
        console_ui.warning("No test command configured, so no tests ran. Set one with `argonauta config set-test-command`.")
    if outcome.test_run is not None:
        r = outcome.test_run
        if r.status == "passed":
            console_ui.success(f"Tests passed (exit {r.exit_code})")
        else:
            console_ui.failure(f"Tests {r.status} (exit {r.exit_code})")
            console_ui.panel(r.output_tail or "(no output)", "Test output", style="red")
    if outcome.commit_sha:
        console_ui.success(f"Committed as {outcome.commit_sha[:10]}")
    elif outcome.skipped_commit_reason:
        console_ui.warning(f"Commit skipped: {outcome.skipped_commit_reason}.")
        console_ui.info(f"Fix and `argonauta commit {p.id}`, or undo with `argonauta revert {p.id}`.")
    elif not commit:
        console_ui.info(f"Not committed (--no-commit). Later: `argonauta commit {p.id}` or `argonauta revert {p.id}`.")
    if outcome.test_run is not None and outcome.test_run.status != "passed":
        raise typer.Exit(EXIT_FAIL)


@app.command()
def test(proposal_id: Annotated[Optional[int], typer.Argument(help="Attach the run to this proposal.", show_default=False)] = None) -> None:
    """Run the configured test command now and record the result."""
    from argonauta.workflow import run_tests_now

    settings = _settings()
    store = _store(settings)
    con = console_ui.console()
    if con.is_terminal:
        with con.status("running tests…", spinner="dots"):
            run = _run(run_tests_now, settings, store, proposal_id)
    else:
        run = _run(run_tests_now, settings, store, proposal_id)
    store.close()
    if run.status == "passed":
        console_ui.success(f"Tests passed (exit {run.exit_code}), recorded as run {run.id}")
    else:
        console_ui.failure(f"Tests {run.status} (exit {run.exit_code}), recorded as run {run.id}")
        console_ui.panel(run.output_tail or "(no output)", "Test output", style="red")
        raise typer.Exit(EXIT_FAIL)


@app.command()
def commit(proposal_id: Annotated[int, typer.Argument(help="Applied proposal to commit.")]) -> None:
    """Commit an applied-but-uncommitted proposal."""
    from argonauta.workflow import commit_change

    settings = _settings()
    store = _store(settings)
    sha = _run(commit_change, settings, store, proposal_id)
    store.close()
    console_ui.success(f"Committed proposal {proposal_id} as {sha[:10]}")


@app.command()
def reject(
    proposal_id: Annotated[int, typer.Argument(help="Proposal to discard.")],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Why (kept in history).")] = "",
) -> None:
    """Discard a pending proposal without touching the working tree."""
    from argonauta.workflow import reject_proposal

    settings = _settings()
    store = _store(settings)
    if len(reason) > 500:
        store.close()
        console_ui.failure("--reason must be 500 characters or fewer.")
        raise typer.Exit(EXIT_INVALID)
    p = _run(reject_proposal, store, proposal_id, reason)
    store.close()
    console_ui.success(f"Rejected proposal {p.id}")


@app.command()
def revert(
    proposal_id: Annotated[int, typer.Argument(help="Applied proposal to undo.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Undo an applied proposal (git revert if committed, reverse-apply otherwise)."""
    from argonauta.workflow import revert_proposal

    settings = _settings()
    store = _store(settings)
    if not _confirm(f"Revert proposal {proposal_id}?", yes):
        console_ui.info("Nothing changed.")
        store.close()
        raise typer.Exit(EXIT_OK)
    change, sha = _run(revert_proposal, settings, store, proposal_id)
    store.close()
    console_ui.success(f"Reverted change {change.id}" + (f" with commit {sha[:10]}" if sha else " (working tree restored)"))


# ----- history / export / import ------------------------------------------------
@app.command()
def history(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500, help="How many proposals to show.")] = 20,
    events: Annotated[bool, typer.Option("--events", help="Also list the raw event log.")] = False,
) -> None:
    """List proposals with their change, test and commit outcome, plus recent git commits."""
    settings = _settings()
    store = _store(settings)
    proposals = store.list_proposals(limit=limit)
    if not proposals:
        console_ui.empty_state("no proposals", 'Run `argonauta propose "<task>"` to create the first one, or `argonauta sample load` to see what history looks like.')
    else:
        t = console_ui.table("Proposals", ["id", "created", "status", "tests", "commit", "task"])
        for p in proposals:
            change = store.change_for_proposal(p.id)
            run = store.latest_test_run_for_proposal(p.id)
            t.add_row(
                str(p.id),
                p.created_at[:16].replace("T", " "),
                _status_text(p.status),
                _status_text(run.status) if run else "–",
                (change.commit_sha[:8] if change and change.commit_sha else "–") + (" ↩" if change and change.reverted_at else ""),
                console_ui.short(p.task, 50),
            )
        console_ui.print_table(t)
    if events:
        evs = store.list_events(limit=limit * 3)
        t = console_ui.table("Events", ["ts", "kind", "ref", "message"])
        for e in evs:
            t.add_row(e.ts[:19].replace("T", " "), e.kind, f"{e.ref_table}#{e.ref_id}" if e.ref_table else "", console_ui.short(e.message, 70))
        console_ui.print_table(t)
    store.close()
    log = gitops.recent_log(settings.repo_root, limit=5)
    if log:
        t = console_ui.table("Recent commits", ["sha", "when", "subject"])
        for e in log:
            t.add_row(e.short, e.when[:16], console_ui.short(e.subject, 60))
        console_ui.print_table(t)


@app.command("export")
def export_cmd(path: Annotated[Path, typer.Argument(help="Where to write the JSON export.")]) -> None:
    """Export every record (proposals, changes, test runs, events, settings) to JSON."""
    from argonauta.exchange import export_to_file

    settings = _settings()
    store = _store(settings)
    if path.is_dir():
        store.close()
        console_ui.failure(f"{path} is a directory; give a file path.")
        raise typer.Exit(EXIT_INVALID)
    try:
        counts = export_to_file(store, path)
    except OSError as exc:
        store.close()
        _fail(RuntimeError(f"Could not write {path}: {exc.strerror}"))
    store.record_event("export", f"exported to {path.name}")
    store.close()
    console_ui.success(f"Exported to {path}: " + ", ".join(f"{n} {k}" for k, n in counts.items()))


@app.command("import")
def import_cmd(
    path: Annotated[Path, typer.Argument(help="JSON file produced by `argonauta export`.")],
    replace: Annotated[bool, typer.Option("--replace", help="Delete existing records first instead of merging.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation when replacing.")] = False,
) -> None:
    """Import records from a JSON export (merge by default)."""
    from argonauta.exchange import ExchangeError, import_data, load_export

    settings = _settings()
    if not path.is_file():
        console_ui.failure(f"{path} is not a file.")
        raise typer.Exit(EXIT_INVALID)
    try:
        data = load_export(path)
    except ExchangeError as exc:
        console_ui.failure(f"Invalid export: {exc}")
        raise typer.Exit(EXIT_INVALID)
    store = _store(settings)
    if replace and not _confirm("Replace ALL existing proposals, changes, test runs and events?", yes):
        console_ui.info("Nothing changed.")
        store.close()
        raise typer.Exit(EXIT_OK)
    try:
        with store.transaction():
            report = import_data(store, data, replace=replace)
    except ExchangeError as exc:
        store.close()
        console_ui.failure(f"Import aborted, nothing changed: {exc}")
        raise typer.Exit(EXIT_INVALID)
    store.close()
    console_ui.success(f"Imported {report.proposals} proposals, {report.changes} changes, {report.test_runs} test runs, {report.events} events")
    if report.skipped_settings:
        console_ui.warning("Settings are never imported because the test command is executed. The file contained:")
        for key, value in report.skipped_settings.items():
            console_ui.info(f"  {key} = {console_ui.short(value, 100)}")
        console_ui.info('Set it deliberately with `argonauta config set-test-command "<command>"` if you trust it.')


# ----- sample / config ------------------------------------------------------------
@sample_app.command("load")
def sample_load() -> None:
    """Insert labelled sample proposals, changes and test runs."""
    from argonauta.sample import load_samples

    settings = _settings()
    store = _store(settings)
    created = load_samples(store)
    store.close()
    console_ui.success("Loaded sample data: " + ", ".join(f"{n} {k}" for k, n in created.items()))
    console_ui.info("Every sample row is marked [SAMPLE]; remove them with `argonauta sample clear`.")


@sample_app.command("clear")
def sample_clear() -> None:
    """Delete every sample record."""
    from argonauta.sample import clear_samples

    settings = _settings()
    store = _store(settings)
    counts = clear_samples(store)
    store.close()
    console_ui.success("Removed sample data: " + ", ".join(f"{n} {k}" for k, n in counts.items()))


@config_app.command("show")
def config_show() -> None:
    """Print effective settings (never secrets)."""
    from argonauta.workflow import test_command_for

    settings = _settings()
    t = console_ui.table("Settings", ["key", "value", "source"])
    t.add_row("provider", describe_provider(settings), "env ARGONAUTA_PROVIDER" if settings.provider != "auto" else "auto")
    t.add_row("api key", "present" if settings.has_api_key else "absent", ".env / environment")
    t.add_row("model", settings.model, "ARGONAUTA_MODEL")
    t.add_row("effort", settings.effort, "ARGONAUTA_EFFORT")
    t.add_row("fallbacks", settings.fallbacks, "ARGONAUTA_FALLBACKS")
    t.add_row("max context", f"{settings.max_context_files} files / {settings.max_context_bytes} bytes", "ARGONAUTA_MAX_CONTEXT_*")
    t.add_row("test timeout", f"{settings.test_timeout}s", "ARGONAUTA_TEST_TIMEOUT")
    if settings.db_path.is_file():
        store = _store(settings)
        t.add_row("test command", test_command_for(settings, store) or "not set", "ARGONAUTA_TEST_COMMAND or store")
        store.close()
    t.add_row("data dir", str(settings.data_dir), "fixed")
    console_ui.print_table(t)


@config_app.command("set-test-command")
def config_set_test_command(command: Annotated[str, typer.Argument(help='e.g. "python -m pytest -q"')]) -> None:
    """Store the command that `apply` and `test` run."""
    from argonauta.runner import RunnerError, check_command
    from argonauta.workflow import TEST_COMMAND_KEY

    settings = _settings()
    try:
        check_command(command)
    except RunnerError as exc:
        console_ui.failure(str(exc))
        raise typer.Exit(EXIT_INVALID)
    store = _store(settings)
    store.set_setting(TEST_COMMAND_KEY, command)
    store.record_event("config", "test command updated")
    store.close()
    console_ui.success(f"Test command saved: {command}")


@workspace_app.command("list")
def workspace_list() -> None:
    """List workspaces and their repositories."""
    from argonauta.workspaces import RegistryError, open_registry

    try:
        registry = open_registry()
    except RegistryError as exc:
        _fail(exc, EXIT_INVALID)
    if not registry.workspaces:
        console_ui.empty_state("no workspaces", "Add one with `argonauta workspace add PATH` or from the web UI (`argonauta ui`).")
        return
    t = console_ui.table("Workspaces", ["workspace", "repository", "path", "store"])
    for ws in registry.workspaces:
        if not ws.repos:
            t.add_row(ws.name, "(empty)", "", "")
        for r in ws.repos:
            t.add_row(ws.name, r.name, str(r.path), ("initialised" if r.initialized else "not initialised") if r.exists else "missing")
    console_ui.print_table(t)
    console_ui.info(f"Registry: {registry.path}")


@workspace_app.command("add")
def workspace_add(
    path: Annotated[Path, typer.Argument(help="Path inside a git repository.")],
    workspace: Annotated[str, typer.Option("--workspace", "-w", help="Workspace name (created if missing).")] = "Default",
) -> None:
    """Register a repository in a workspace."""
    from argonauta.workspaces import RegistryError, open_registry

    try:
        registry = open_registry()
        ws = next((w for w in registry.workspaces if w.name == workspace), None) or registry.add_workspace(workspace)
        repo = registry.add_repo(ws.id, path)
    except RegistryError as exc:
        _fail(exc, EXIT_INVALID)
    console_ui.success(f"Added {repo.path} to workspace {ws.name!r} as {repo.id}")


@workspace_app.command("remove")
def workspace_remove(path: Annotated[Path, typer.Argument(help="Path of a registered repository.")]) -> None:
    """Remove a repository from every workspace."""
    from argonauta.workspaces import RegistryError, open_registry

    try:
        registry = open_registry()
        repo = registry.find_repo_by_path(path)
        if repo is None:
            raise RegistryError(f"{path} is not registered")
        registry.remove_repo(repo.id)
    except RegistryError as exc:
        _fail(exc, EXIT_INVALID)
    console_ui.success(f"Removed {repo.path}")


@app.command()
def ui(
    port: Annotated[int, typer.Option("--port", "-p", min=0, max=65535, help="Port to listen on (0 = pick a free port).")] = 0,
    host: Annotated[str, typer.Option("--host", help="127.0.0.1 or localhost only.")] = "127.0.0.1",
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the page in your browser.")] = True,
) -> None:
    """Start the local web UI (commit graph, staging, branches, terminal, assistant)."""
    from argonauta.web.app import run_ui

    if host not in ("127.0.0.1", "localhost"):
        console_ui.failure("The UI only listens on 127.0.0.1 or localhost; it is a single-user tool.")
        raise typer.Exit(EXIT_INVALID)
    start_repo: Path | None = None
    try:
        start_repo = gitops.repo_toplevel((state.repo_override or Path.cwd()).resolve())
    except gitops.GitError:
        start_repo = None  # the page lets you add repositories
    console_ui.info("Press Ctrl+C to stop. Everything stays on this machine.")
    try:
        code = run_ui(host=host, port=port, open_browser=open_browser, start_repo=start_repo, announce=lambda line: (typer.echo(line), sys.stdout.flush()))
    except ValueError as exc:
        console_ui.failure(str(exc))
        raise typer.Exit(EXIT_INVALID)
    except OSError as exc:
        console_ui.failure(f"Could not start the server: {exc.strerror or exc}")
        raise typer.Exit(EXIT_FAIL)
    raise typer.Exit(code)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console_ui.failure("Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
