# Argonauta

A repository-local Git client assistant for one person. It indexes one codebase,
asks one model for a diff, runs your tests, and records every step in a private
SQLite file inside the repository. It replaces the core loop of a desktop Git
client (look at the repo, make a change, run tests, commit, read history) with a
keyboard-driven command line.

Stack: Python 3.12, Typer, SQLite. No accounts, no analytics, no telemetry, no ads.

```
argonauta init --test-command "python -m pytest -q"
argonauta index
argonauta propose "Make power() handle negative exponents"
argonauta apply 1          # applies the diff, runs the tests, commits on green
argonauta history          # proposals, test results, commits, event log
argonauta export backup.json
```

## Setup

Requirements: Python 3.12 or newer, git 2.24 or newer, macOS or Linux (Windows via WSL).

```
scripts/install.sh         # creates .venv, installs argonauta[anthropic,dev], copies .env.example to .env
scripts/dev.sh --help      # run from source
```

Optional: put an Anthropic API key in `.env` to let `propose` call a model.
Without one, Argonauta runs in **manual mode** (see "Degraded mode" below) and
never opens a network connection.

`.env.example` documents every variable. The defaults are:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables the model provider. Only checked for presence, never logged. |
| `ARGONAUTA_PROVIDER` | `auto` | `auto`, `anthropic`, `manual`, or `scripted` (test only). Shell only. |
| `ARGONAUTA_MODEL` | `claude-opus-5-5` | Model id for proposals. |
| `ARGONAUTA_EFFORT` | `medium` | `low` to `max`; controls reasoning depth and cost. |
| `ARGONAUTA_FALLBACKS` | `default` | Server-side refusal fallbacks (`default` or `off`). |
| `ARGONAUTA_MAX_CONTEXT_FILES` | `8` | Cap on files sent with a task. |
| `ARGONAUTA_MAX_CONTEXT_BYTES` | `60000` | Cap on total bytes sent with a task. |
| `ARGONAUTA_TEST_COMMAND` | unset | Overrides the test command saved by `init`. Shell only. |
| `ARGONAUTA_TEST_TIMEOUT` | `600` | Seconds before a test run is killed. |

## The core loop

1. **`init`** creates `.argonauta/` in the repository root and adds it to
   `.git/info/exclude` so it never shows up in `git status`. Pass
   `--test-command` or set it later with `config set-test-command`.
2. **`index`** walks the files git knows about (tracked plus untracked, minus
   ignored), skips binaries, files over 512 KB, `.env*`, `.git/` and
   `.argonauta/`, and builds a term index in SQLite. Re-running is incremental.
3. **`propose "<task>"`** ranks indexed files against the task with BM25, packs
   the best ones within the byte and file caps, and asks the provider for a
   diff. `--dry-run` prints exactly which files would be sent and stops.
   The reply is parsed, validated (relative paths only, no `..`, nothing under
   `.git/`, `.argonauta/` or `.env*`, must pass `git apply --check`) and stored
   as a proposal with status `proposed` or `invalid`.
4. **`show <id>`** prints the explanation, the diff and any test runs.
5. **`apply <id>`** shows the diff, asks for confirmation (`--yes` skips it),
   refuses if the touched files have uncommitted changes, applies the diff,
   runs the test command, and commits when the tests pass. On red it leaves the
   change in the working tree, records the failure, and tells you to fix and
   `commit` or `revert`. `--no-test` and `--no-commit` are available.
6. **`test [id]`**, **`commit <id>`**, **`revert <id>`**, **`reject <id>`**
   cover the rest of the loop. `revert` uses `git revert` for committed changes
   and a reverse `git apply` for uncommitted ones.
7. **`history`** lists proposals with test and commit outcome plus recent git
   commits; `--events` adds the raw audit log. **`status`** shows the branch,
   working tree, index, provider and test command.

Every command has a clear state: an empty state that names the next command,
a loading spinner or progress bar on a terminal, validation errors (exit code 2),
success lines prefixed with `✓` (exit 0) and failures prefixed with `✗` (exit 1).

## Web UI

`argonauta ui` starts a local server on 127.0.0.1, prints a one-time URL with a
token and opens it in your browser. The page has the GitKraken-style layout:
workspaces and branches on the left, the commit graph in the middle, diffs and
details on the right, and proposals, events and terminals at the bottom.

What you can do there: stage, unstage and discard files ("discard" drops both
the staged and the working-tree change, and deletes files git has never
committed); commit (with amend);
create, switch and delete local branches; fetch, pull and push; open terminal
tabs; read the credentials panel; and run the whole assistant loop (initialize,
index, propose, apply with tests, commit, reject, revert).

Shortcuts: `p` proposal field, `c` commit message, `t` new terminal, `/`
filter commits, `?` help, `Esc` closes dialogs. Every control is reachable with
Tab and has a visible focus ring; status is always shown with a symbol, never
colour alone; the theme follows your OS and both palettes are chosen for AA
contrast.

Stop the server with Ctrl+C. Nothing runs when it is not running.

## Workspaces

A workspace is a named group of repositories. The list lives in
`~/.config/argonauta/workspaces.json` (override with `ARGONAUTA_CONFIG_DIR`).
Each repository keeps its own `.argonauta/` store. Manage them in the page or
with `argonauta workspace list | add PATH | remove PATH`. Running
`argonauta ui` inside an unregistered repository adds it to "Default".

## Credentials panel

Shows the git identity (`user.name`, `user.email`) and where it comes from, the
SSH host alias your remote resolves to, which identity files `ssh -G` would try,
their type and fingerprint, whether the key is loaded in `ssh-add -l`, the
commit-signing key (GPG or SSH) with its validity, and the signature status of
HEAD and of every commit in the graph. It reads `git config`, `ssh -G`,
`ssh-keygen -l` on `.pub` files, `ssh-add -l` and `gpg --list-keys`. It never
opens a private key.

## Embedded terminal

A terminal tab is a real login shell started inside the repository with your
normal environment. It is wired to the page through a WebSocket on the same
loopback server. This is the most sensitive feature in Argonauta: anyone who
can reach the server with the token can run commands as you. The protections
are that the server listens only on 127.0.0.1, every WebSocket must present the
per-run token as its first message and a matching Origin, shells die when the
tab or the server closes, and the server only exists while `argonauta ui` runs.
If you do not want this at all, do not open terminal tabs; nothing is spawned
until you do.

## Permissions: what Argonauta touches and what leaves the machine

- **Reads** files that `git ls-files` reports inside the repository, and nothing
  outside it. Symlinks are skipped by the indexer and rejected in diffs.
- **Writes** only inside `.argonauta/` plus whatever an *applied* diff changes.
  Applying always goes through `git apply`, and committing uses your own git
  identity and hooks.
- **Runs** the test command you configured, parsed with `shlex` and executed
  without a shell, with a timeout, and with `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` removed from its environment.
- **Web UI**: the page talks only to the loopback server that `argonauta ui`
  started, with a per-run token; it loads no external script, font or image.
- **Network**: exactly one kind of request, to the Anthropic API, and only when
  a key is present and you run `propose` without `--dry-run` or `--from-diff`.
  The request contains the system prompt, your task text, a short repository
  summary (name, branch, file count, languages) and the contents of the selected
  context files. Use `propose --dry-run` to see that list first. Nothing else is
  ever sent anywhere.
- **`.env` is read with an allowlist.** Only `ANTHROPIC_API_KEY` and the
  non-executable `ARGONAUTA_*` tuning keys are loaded from the file; your shell
  always wins over it, and the API endpoint is pinned in code. The test command,
  provider choice and scripted-diff path are shell-only, so a cloned repository
  cannot redirect requests, inject proxy settings, or choose what `apply`
  executes through its `.env`.
- **Logs** (`.argonauta/argonauta.log`) contain ids, counts, paths and status
  words. They never contain diffs, file contents or secrets; a redacting filter
  scrubs key-shaped strings as a last resort.

## Degraded mode (no API key)

With no key, `propose` writes a prompt bundle to `.argonauta/prompts/` containing
the system prompt and the same user message the model would receive. Paste it
into any model you like, save the reply to a file, and ingest it with:

```
argonauta propose "<same task>" --from-diff reply.md
```

The reply goes through the same extraction, validation and `git apply --check`
as a live reply, and the rest of the loop (apply, test, commit, revert,
history, export) is unchanged. `status` shows a warning while in this mode.

If the key is present but the `anthropic` package is missing, `propose` says so
and points at `pip install 'argonauta[anthropic]'`.

## Architecture

```
argonauta/
  cli.py                 Typer commands; renders empty/loading/validation/success/failure states
  config.py              Settings from .env and the environment; repo root discovery
  db.py                  SQLite store: schema, nesting-aware transactions, typed accessors
  models.py              Dataclasses for rows and the status enums
  gitops.py              argv-only git wrappers (status, apply, commit, revert, exclude)
  indexer.py             git ls-files walk, tokenizer, inverted index, BM25 search
  diffs.py               extract / parse / validate unified diffs from untrusted text
  prompts.py             system prompt, context packing, reply parsing, manual bundle
  providers/             anthropic (SDK, streaming, adaptive thinking, refusal fallbacks),
                         manual (degraded), scripted (test only)
  runner.py              test command execution without a shell
  workflow.py            propose -> apply -> test -> commit / revert, with audit events
  exchange.py            JSON export and validated import (merge or replace)
  sample.py              labelled sample records
  ui.py                  Rich console helpers with symbol + colour state markers
  logging_setup.py       rotating, redacting file logger
```

Data model (one SQLite file per repository):

| Table | Holds |
| --- | --- |
| `meta`, `settings` | schema version, repo root, the test command |
| `files`, `terms` | index metadata (path, size, hash, language) and term counts |
| `proposals` | task, provider, model, status, explanation, diff, context files, token counts |
| `changes` | applied files, line counts, commit sha, reverted timestamp |
| `test_runs` | command, exit code, status, output tail, link to proposal and change |
| `events` | append-only audit log of every step |

## Data location, backup, and leaving

- Everything lives in `<repo>/.argonauta/`: `argonauta.db` (SQLite, WAL mode),
  `argonauta.log`, and `prompts/` for manual-mode bundles. Delete the folder to
  remove every trace; the repository itself is untouched.
- The workspace list for the web UI lives in `~/.config/argonauta/workspaces.json`
  (paths and names only). Delete it to forget every workspace.
- `argonauta export backup.json` writes all proposals, changes, test runs,
  events, settings and index metadata (paths and hashes, never file contents) as
  plain JSON. Diffs are included because they are your work.
- `argonauta import backup.json` merges records with fresh ids;
  `--replace` wipes the existing records first and asks for confirmation. Imports
  are validated field by field and applied atomically. Settings, including the
  test command, are never imported because `apply` executes that command; the
  CLI shows what the file contained so you can set it on purpose.
- The SQLite file can also be copied while the CLI is not running, or inspected
  with any SQLite tool.

## Sample data

`argonauta sample load` inserts three `[SAMPLE]`-prefixed proposals (applied,
rejected, pending with a failed test run) so you can see what history looks like.
`argonauta sample clear` deletes them; sample proposals can never be applied.

`examples/sample_project/` is a tiny calculator with one deliberate bug, and
`examples/sample_reply.md` is a model-shaped reply that fixes it. `scripts/demo.sh`
copies the project to a temporary directory and runs the whole loop with the
test-only `scripted` provider, so you can watch it work without a key.

## Accessibility

The interface is a terminal program, so keyboard use is the only mode. Beyond
that: every state is marked with a symbol (`✓`, `✗`, `!`, `·`, `○`, `↩`) as well
as a colour, so nothing depends on colour alone; `--plain` and the `NO_COLOR`
environment variable turn colour and spinners off; tables carry column headers;
every command and option has a help string reachable with `-h`; confirmations
state what will happen and default to "no"; and when stdin is not a terminal
the CLI refuses to guess and asks for `--yes` instead of hanging.

## Scripts

| Script | Purpose |
| --- | --- |
| `scripts/install.sh` | create `.venv`, install with extras, create `.env` |
| `scripts/dev.sh ...` | run the CLI from source |
| `scripts/test.sh` | run unit tests and the end-to-end smoke test |
| `scripts/build.sh` | build wheel and sdist into `dist/`, run `pip check` |
| `scripts/run.sh ...` | production-style run: install the built wheel into `.venv-prod` and run it |
| `scripts/demo.sh` | run the full loop on a throwaway copy of the sample project |
| `scripts/dev.sh ui` | start the web UI from source |

## Tests

`scripts/test.sh` runs 120 tests: store round trips and cascade deletes, diff
extraction and path validation (traversal, absolute paths, protected files,
symlinks), tokenizer and BM25 ranking with incremental re-indexing, reply parsing,
export/import round trips with rejection of malformed payloads and atomicity,
the workflow against a temporary git repository (propose, apply, test, commit,
revert, failed tests blocking commits, dirty-tree refusal, invalid diffs), log
redaction, one end-to-end smoke test that drives the installed CLI as a
subprocess through init, index, propose, apply, show, history, export, import
and sample load/clear, plus the web layer: workspace registry, job runner,
WebSocket framing, pty sessions, credential parsing, graph lanes, the JSON API
against a live server thread (auth failures, path traversal, staging, commits,
branches, remote jobs against a bare repository, the assistant loop), a
WebSocket terminal round trip, and a smoke test that starts `argonauta ui` as a
subprocess and stops it with Ctrl+C.

## Limitations (on purpose)

- **Model quality is whatever model you point it at.** Argonauta does not claim
  frontier coding quality, and the default model can be changed in `.env`.
- **Context is small and lexical.** Retrieval is BM25 over identifiers, capped
  at a handful of files and tens of kilobytes. There are no embeddings, no
  repository-wide context windows and no multi-file planning.
- **No IDE integration, no GUI, no background daemon.** Latency is a normal
  CLI plus one model call.
- **Single user, single repository per store.** There is no sync, sharing or
  multi-user history.
- **Unified diffs only.** Replies without a parseable diff are stored as
  `invalid` with the model's explanation so you can read why.
- **Not a full Git client.** The web UI covers staging, commits, local
  branches, fetch, pull and push. Merge, rebase, conflict resolution, stash,
  hunk-level staging, tags and submodules stay with `git` itself.
- **The web UI is single-user.** It binds to 127.0.0.1 only and must never be
  exposed beyond localhost; the embedded terminal is a real shell.
- **No security certification.** Input validation and the protected-path rules
  are tested, but this is a personal tool: review every diff before `apply`.

## License

GPL-3.0-or-later, see `LICENSE`.
