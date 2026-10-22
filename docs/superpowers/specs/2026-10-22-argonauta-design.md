# Argonauta design (2026-10-22)

## Intent

Argonauta is a repository-local Git client assistant. It replaces the core loop a
developer runs inside a GUI Git client (look at the repo, make a change, run the
tests, commit, look at history) with a single-user, private, keyboard-driven CLI
that can also ask one model to draft the change as a diff.

Success means a user can, from a terminal, in one repo:

1. `init` a private data store inside the repo,
2. `index` the codebase,
3. `propose` a change for a task (model-drafted, or hand-supplied in degraded mode),
4. review the diff, `apply` it, have the tests run automatically,
5. `commit` on green, `revert` on red,
6. see every step in `history`, and `export` / `import` all of it as JSON.

## Constraints (from the brief)

- Python 3.12 + Typer + SQLite, nothing else as a hard runtime dependency except
  `python-dotenv` and `rich` (Typer's own output library).
- Single-user, private by default. All data lives in `<repo>/.argonauta/`.
- No analytics, telemetry, ads, or third-party accounts. The only network call is
  the optional model request, and only when a key is present.
- Secrets live in `.env`; `.env.example` documents them. Logs never contain
  secrets, diffs, or file contents.
- The app must stay useful without an API key (degraded "manual" provider).
- Excluded on purpose: frontier model quality claims, large-context infrastructure
  (context is capped and lexical), IDE-wide polish and latency.

## Architecture

```
argonauta/
  cli.py          Typer commands; owns the user-facing states (empty/loading/
                  validation/success/failure) via ui.py
  config.py       Settings from .env + environment; repo root discovery
  db.py           SQLite store: schema, migrations, typed accessors
  models.py       Dataclasses for rows
  gitops.py       Thin subprocess wrappers over git (no shell)
  indexer.py      File walk (git ls-files), tokenizer, inverted index, BM25 search
  diffs.py        Extract, parse, validate unified diffs from untrusted text
  prompts.py      System prompt, context packing, response parsing
  providers/      base (protocol), anthropic (real model), manual (degraded),
                  scripted (test-only, reads a diff from a file)
  runner.py       Runs the configured test command without a shell
  workflow.py     The core loop: propose -> apply -> test -> commit / revert
  exchange.py     JSON export and import with validation
  sample.py       Clearly labelled sample records, loadable and deletable
  ui.py           Rich console helpers with non-colour-only state markers
  logging_setup.py Redacting file logger
```

Data flow for `propose`: task text -> validate -> BM25 search over the index ->
pick top files within byte/file caps -> provider -> extract diff -> validate paths
and `git apply --check` -> store proposal (status `proposed` or `invalid`).

Data flow for `apply`: load proposal -> ensure touched files are clean ->
`git apply` -> record change -> run tests -> record test run -> commit on green
(unless `--no-commit`) -> events written at each step.

## Data model (SQLite)

- `meta(key, value)` schema version, repo root, created_at.
- `settings(key, value)` test_command and other per-repo config.
- `files(id, path, size, sha256, language, line_count, indexed_at)`.
- `terms(term, file_id, count)` inverted index, `file_id` FK cascade.
- `proposals(id, created_at, task, provider, model, status, explanation, diff,
  context_files, input_tokens, output_tokens, notes, is_sample)`.
- `changes(id, proposal_id, applied_at, files, insertions, deletions,
  commit_sha, reverted_at, is_sample)`.
- `test_runs(id, proposal_id, change_id, started_at, finished_at, command,
  exit_code, status, output_tail, is_sample)`.
- `events(id, ts, kind, ref_table, ref_id, message)` the audit log.

Statuses: proposal `proposed|applied|rejected|invalid|reverted`; test run
`passed|failed|error`.

## Providers

- `anthropic`: official SDK, streaming, adaptive thinking, explicit effort,
  server-side refusal fallbacks on by default. Default model `claude-opus-5-5`.
- `manual` (degraded): writes a prompt bundle to `.argonauta/prompts/` for the
  user to paste into any model; the reply is ingested with `propose --from-diff`.
- `scripted` (test-only): returns the diff in `ARGONAUTA_SCRIPTED_DIFF`. Status
  output labels it as not a real model.

## Input validation

- Task text: 1..4000 chars, printable.
- Diffs from any provider: must parse as unified diff; paths relative, no `..`,
  no absolute paths, never under `.git/`, `.argonauta/`, or `.env*`; size cap;
  then `git apply --check`.
- Test command: parsed with `shlex`, run without a shell, with a timeout, with
  `ANTHROPIC_*` variables stripped from the child environment.
- Import JSON: format marker, version, per-record type checks, status enums.

## Testing

- Unit: store round trips, export/import, diff extraction and validation,
  tokenizer and search ranking, response parsing, workflow against a temp git repo
  with the scripted provider.
- End-to-end smoke: subprocess runs the CLI on a copy of `examples/sample_project`
  in a temp git repo: init, index, propose, apply (tests run), history, export,
  import into a fresh store.

## Interface states

Every command renders one of: empty (nothing indexed / no proposals, with the
next command to run), loading (spinner or progress bar), validation error (exit
2, message names the field), success (`✓` prefix, exit 0), failure (`✗` prefix,
reason, exit 1). Markers are symbols plus colour, never colour alone. `--plain`
and `NO_COLOR` disable colour.
