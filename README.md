# Argonauta

A personal, repository-local Git client with an assistant built in. It has two
faces over the same core:

- a **web UI** in the GitKraken mould: workspaces of repositories, a commit
  graph, staging and commits, branches and remotes, an embedded terminal, a
  credentials panel, and the assistant loop on the same page;
- a **command line** for the same loop when you would rather type.

The assistant indexes one codebase, asks one model for a diff, runs your tests,
commits on green, and records every step in a private SQLite file inside the
repository. Nothing leaves your machine except the optional model request.

Stack: Python 3.12, Typer, SQLite, a standard-library HTTP server and vanilla
JavaScript. No accounts, no analytics, no telemetry, no ads, no CDN.

```
scripts/install.sh                     # one-time setup
argonauta ui                           # opens the web UI for the current repository

argonauta init --test-command "python -m pytest -q"
argonauta index
argonauta propose "Make power() handle negative exponents"
argonauta apply 1                      # applies the diff, runs the tests, commits on green
argonauta history
```

## Setup

Requirements: Python 3.12 or newer, git 2.24 or newer, macOS or Linux (Windows
via WSL). A browser for the web UI; any recent Chrome, Firefox or Safari works.

```
scripts/install.sh         # creates .venv, installs argonauta[anthropic,dev], copies .env.example to .env
scripts/dev.sh --help      # run the CLI from source
scripts/dev.sh ui          # run the web UI from source
```

The assistant needs a model. Four ways; `auto` tries the first two in order
and falls back to manual:

1. an Anthropic API key in `.env` (the `anthropic` provider, pay per token);
2. a model running on your machine or your LAN behind an OpenAI-compatible
   API, such as Ollama, LM Studio, llama.cpp server or vLLM (the `local`
   provider; see "Local models" below);
3. the installed Claude Code CLI under your Claude subscription (the
   `claude-cli` provider; chosen explicitly, see "Claude Code CLI" below);
4. **manual mode** (see "Degraded mode" below), which never opens a network
   connection.

The **Model** button in the web UI switches between them.

`.env.example` documents every variable. "Shell only" means the variable is
ignored when it appears in a repository's `.env` and must come from your shell,
because a cloned repository must not be able to choose what runs or where
settings live.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables the model provider. Only checked for presence, never logged. |
| `ARGONAUTA_MODEL` | `claude-opus-5-5` | Model id for proposals. |
| `ARGONAUTA_EFFORT` | `medium` | `low` to `max`; controls reasoning depth and cost. |
| `ARGONAUTA_FALLBACKS` | `default` | Server-side refusal fallbacks (`default` or `off`). |
| `ARGONAUTA_MAX_CONTEXT_FILES` | `8` | Cap on files sent with a task. |
| `ARGONAUTA_MAX_CONTEXT_BYTES` | `60000` | Cap on total bytes sent with a task. |
| `ARGONAUTA_TEST_TIMEOUT` | `600` | Seconds before a test run is killed. |
| `ARGONAUTA_LOCAL_MODEL` | unset | Model name for the local provider, e.g. `qwen2.5-coder:7b`. |
| `ARGONAUTA_LOCAL_API_KEY` | unset | Bearer token for local servers that require one. |
| `ARGONAUTA_PROVIDER` | `auto` | `auto`, `anthropic`, `local`, `claude-cli`, `manual`, or `scripted` (test only). Shell only. |
| `ARGONAUTA_CLAUDE_BIN` | unset | Path to the `claude` binary when it is not on PATH. Shell only. |
| `ARGONAUTA_LOCAL_URL` | `http://127.0.0.1:11434/v1` | Base URL of the OpenAI-compatible API. Loopback only unless allowed. Shell only. |
| `ARGONAUTA_LOCAL_ALLOW_REMOTE` | unset | Set to `1` to allow a non-loopback `ARGONAUTA_LOCAL_URL`. Shell only. |
| `ARGONAUTA_TEST_COMMAND` | unset | Overrides the test command saved by `init`. Shell only. |
| `ARGONAUTA_CONFIG_DIR` | `~/.config/argonauta` | Where the workspace registry lives. Shell only. |
| `ARGONAUTA_SCRIPTED_DIFF` | unset | Reply file read by the test-only `scripted` provider. Shell only. |

The local-model values can also be set in the web UI ("Model" button); those
live in `~/.config/argonauta/settings.json` and shell variables override them.

## Web UI

```
argonauta ui                 # from inside a repository: registers it and opens the browser
argonauta ui --no-open       # print the URL only
argonauta ui --port 8765     # fixed port (default: a free one)
```

The command starts a server on 127.0.0.1, prints a one-time URL carrying a
session token, and opens it in your browser. Stop it with Ctrl+C; nothing runs
when it is not running.

### Layout

```
┌ workspace ▾ · repository ▾ · branch pill · Fetch Pull Push · Terminal Credentials ? ┐
├ left ─────────┬ centre ──────────────────────┬ right ───────────────────────────────┤
│ Workspaces    │ Commits: graph with lanes,   │ Detail: diff of the selected file,  │
│ Branches      │ refs, signature badge, sha;  │ commit or proposal                  │
│ Remotes       │ filter box                   │ Credentials: identity, SSH, signing │
│ Changes       │                              │                                      │
│  staged /     │                              │                                      │
│  unstaged     │                              │                                      │
│  commit box   │                              │                                      │
├ bottom ───────┴──────────────────────────────┴──────────────────────────────────────┤
│ tabs: Proposals · Events · one tab per open terminal                                │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Every control has an icon and a text label; the icons are an inline SVG sprite
shipped with the page.

### Workspaces and repositories

A workspace is a named group of repositories. Pick one in the top-left
selector or in the sidebar tree; pick a repository the same way. Everything
else on the page (branches, commits, changes, terminals, credentials, the
assistant) belongs to the selected repository. A workspace with no repositories
says so in place of the graph and offers "Add a repository"; repository-scoped
buttons stay disabled until one is selected.

"+ Workspace" and "+ Repo" open dialogs; "+ Repo" takes an absolute path to any
folder inside a git repository (a git worktree is fine) and registers its
toplevel. The `×` next to a workspace or repository removes it from the list
only; nothing on disk is touched.

The list lives in `~/.config/argonauta/workspaces.json` (names and paths only).
The same list is available from the shell:

```
argonauta workspace list
argonauta workspace add PATH [--workspace NAME]
argonauta workspace remove PATH
```

Running `argonauta ui` inside a repository that is not registered adds it to a
workspace called "Default".

### Changes, commits, branches, remotes

- **Changes** lists staged and unstaged files. Tick a file to stage it, untick
  to unstage, use "Stage all" / "Unstage all", click a file name to see its
  diff. The undo icon discards a file's changes after a confirmation: it drops
  both the staged and the working-tree change and deletes files git has never
  committed.
- **Commit** takes a message (and an "Amend" checkbox) and commits what is
  staged with your own git identity, hooks and signing configuration. Signed
  commits show a green `✓` in the graph.
- **Branches** shows local branches with ahead/behind counts; click one to
  check it out, use the trash icon to delete it (a confirmation asks first,
  and unmerged branches ask again). "+ Branch" creates a branch from HEAD and
  checks it out. Remote branches can be checked out as tracking branches.
- **Fetch, Pull, Push** run in the background and report git's own message.
  Pull is fast-forward only. Push offers to set the upstream when there is
  none. These use your shell's SSH agent or credential helper; a remote that
  needs credentials you do not have fails with git's message instead of
  hanging.
- **Commits** draws the graph with lanes and refs. Click a commit for its
  metadata, signature details, file list and full diff. `/` focuses the filter.

### Embedded terminal

"Terminal" opens a tab with a real login shell started inside the selected
repository, with your normal environment; the tab is named after the
repository and you can open several. It is wired to the page through a
WebSocket on the same loopback server.

This is the most sensitive feature in Argonauta: anyone who can reach the
server with the token can run commands as you. The protections are that the
server listens only on 127.0.0.1, every WebSocket must present the per-run
token as its first message and a matching Origin, shells die when the tab or
the server closes, and the server only exists while `argonauta ui` runs. If you
do not want this at all, do not open terminal tabs; nothing is spawned until
you do.

### Credentials panel

The "Credentials" tab on the right shows, read-only, what git will use for the
selected repository: the git identity (`user.name`, `user.email`) and where it
comes from; the SSH host alias the remote resolves to, the identity files
`ssh -G` would try with their type and fingerprint, and whether the key is
loaded in `ssh-add -l`; the commit-signing key (GPG or SSH) with its validity;
and the signature status of HEAD. The graph marks every commit's signature.
The panel reads `git config`, `ssh -G`, `ssh-keygen -l` on `.pub` files,
`ssh-add -l` and `gpg --list-keys`. It never opens a private key.

### Assistant

The bottom "Proposals" tab is the assistant. A repository without a store
shows an "Initialize" button (optionally with the test command). Then
"Index" scans the files, the task field plus "Propose" asks the provider for a
diff ("Dry run" only shows which files would be sent), and a proposal opens on
the right with its explanation and diff and the actions Apply (with "run
tests" and "commit on green" checkboxes), Reject, Commit and Revert. "Run
tests" runs the configured command on demand. The "Events" tab is the audit
log: every git action taken from the page and every assistant step is recorded
there and in `argonauta history --events`.

Long operations (index, propose, tests, apply, fetch, pull, push) run in the
background with a progress toast; one runs at a time per repository.

### Keyboard and states

Shortcuts: `p` proposal field, `c` commit message, `t` new terminal, `/`
filter commits, `?` help, `Esc` closes dialogs. Every control is reachable with
Tab and has a visible focus ring; arrows move through the commit list and
Enter opens the focused item. Status is always shown with a symbol and text,
never colour alone. The theme follows your OS.

Every panel has an explicit empty state that names the next step, loading
states with spinners, inline validation messages next to fields, and toasts
for success and failure (failure text stays selectable). Destructive actions
(discard, delete branch, revert, remove workspace or repository) always
confirm first.

## Command line

The CLI runs the same loop without a browser.

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
8. **`export`**, **`import`**, **`sample`**, **`config`**, **`workspace`** and
   **`ui`** are described in their own sections. `argonauta -h` lists
   everything.

Every command has a clear state: an empty state that names the next command,
a loading spinner or progress bar on a terminal, validation errors (exit code 2),
success lines prefixed with `✓` (exit 0) and failures prefixed with `✗` (exit 1).

## Local models

Any server that speaks the OpenAI chat-completions API works, with no extra
dependency. With Ollama, for example:

```
ollama pull qwen2.5-coder:7b
export ARGONAUTA_LOCAL_MODEL=qwen2.5-coder:7b      # or put it in .env
argonauta status                                   # provider: local (qwen2.5-coder:7b at http://127.0.0.1:11434/v1 ...)
argonauta propose "Make power() handle negative exponents"
```

In the web UI, the **Model** button in the top bar opens a dialog where you
set the server URL and the model name, test the connection (it lists the
models the server offers) and save. The values go to
`~/.config/argonauta/settings.json`, your own file outside any repository;
shell variables still win over it and are shown as read-only there.

The server may be on another machine, for example a PC with a GPU on your
LAN: tick "allow a server on another machine" and use its address, such as
`http://192.168.0.10:11434/v1`. On that machine Ollama must listen on all
interfaces (`OLLAMA_HOST=0.0.0.0 ollama serve`). Be aware of what that means:
the task and the selected files are sent to that machine, over plain HTTP
unless you put TLS in front of it.

LM Studio listens on `http://127.0.0.1:1234/v1`, llama.cpp server and vLLM on
`http://127.0.0.1:8080/v1` and `http://127.0.0.1:8000/v1` by default. From the
shell, `ARGONAUTA_LOCAL_URL` and `ARGONAUTA_LOCAL_ALLOW_REMOTE=1` do the same
as the dialog. The request carries the same content as for the Anthropic
provider (system prompt, task, repository summary, selected files) and is sent
only to that URL. The reply goes through the same diff extraction and
validation as any other.

Small local models produce rougher diffs than large hosted ones. Argonauta
tolerates the two common defects: hunk headers with wrong line counts (git is
asked to recount them from the hunk body) and context lines that do not match
exactly (a second attempt matches only the line next to each change, and the
proposal is marked "applied with reduced context matching" so you know to
read it closely). Diffs that still do not apply are stored as `invalid` with
the model's explanation. Tested with `qwen2.5-coder:7b` on Ollama: a proposal
takes one to two minutes on an Apple-silicon laptop, and the tests gate the
commit as usual.

## Claude Code CLI (your Claude subscription)

If Claude Code is installed and logged in (`claude auth status`), pick
"Claude Code CLI" in the **Model** dialog or set `ARGONAUTA_PROVIDER=claude-cli`
in your shell. Proposals then run through `claude -p`, the CLI's documented
headless mode, and are covered by your Claude plan instead of API credits.
Argonauta never reads the CLI's credentials: it only starts the command, with
tools disabled, no session persisted, no project settings loaded, in an empty
temporary directory, and passes the same prompt the other providers get on
stdin. Plan rate limits apply, and the CLI's own terms do too; check them if
you rely on this.

## Degraded mode (no API key)

With no key, a proposal writes a prompt bundle to `.argonauta/prompts/`
containing the system prompt and the same user message the model would
receive. The web UI shows the bundle path in a notice; the CLI prints it. Paste
it into any model you like, save the reply to a file, and ingest it with:

```
argonauta propose "<same task>" --from-diff reply.md
```

The reply goes through the same extraction, validation and `git apply --check`
as a live reply, and the rest of the loop (apply, test, commit, revert,
history, export) is unchanged. `status` and the assistant panel both say when
this mode is active.

If the key is present but the `anthropic` package is missing, `propose` says so
and points at `pip install 'argonauta[anthropic]'`.

## Permissions: what Argonauta touches and what leaves the machine

- **Reads** files that `git ls-files` reports inside registered repositories,
  and nothing outside them. Symlinks are skipped by the indexer and rejected in
  diffs. The web UI only ever acts on repositories listed in the workspace
  registry, by id; it never takes a path from the page except to register one.
- **Writes** only inside `.argonauta/`, the workspace registry, and whatever a
  git action you take changes: an applied diff, a stage, a commit, a branch, a
  discard. Applying always goes through `git apply`; committing uses your own
  git identity, hooks and signing setup.
- **Runs** the test command you configured, parsed with `shlex` and executed
  without a shell, with a timeout, and with `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` removed from its environment. Terminal tabs run your
  shell with your full environment, like any terminal.
- **git** is always invoked as an argument list, never through a shell; refs
  and paths coming from the page or the model are checked (no leading dash,
  `--end-of-options`, no `..`, nothing under `.git/`, `.argonauta/` or `.env*`)
  and branch switches use `git switch`, which cannot touch files.
- **Network**: the page talks only to the loopback server that `argonauta ui`
  started, authenticated with a per-run token, and loads no external script,
  font or image. Fetch, pull and push talk to your remotes with your own
  credentials. The model request goes to the Anthropic API or to the local
  model URL, and only when a provider is configured and you propose without
  "Dry run" or `--from-diff`; it contains the system prompt, your task text, a
  short repository summary (name, branch, file count, languages) and the
  contents of the selected context files. Nothing else is ever sent anywhere.
- **`.env` is read with an allowlist.** Only `ANTHROPIC_API_KEY` and the
  non-executable `ARGONAUTA_*` tuning keys are loaded from the file; your shell
  always wins over it, and the API endpoint is pinned in code. The test
  command, provider choice, scripted-diff path, local model URL and registry
  location are shell-only, so a cloned repository cannot redirect requests,
  inject settings, or choose what runs through its `.env`.
- **Logs** (`.argonauta/argonauta.log`) contain ids, counts, paths and status
  words. They never contain diffs, file contents, terminal traffic, tokens or
  secrets; a redacting filter scrubs key-shaped strings as a last resort. The
  web server does not log requests.

## Architecture

```
argonauta/
  cli.py                 Typer commands (init, index, propose, apply, ..., ui, workspace)
  config.py              Settings from .env and the environment; allowlist of .env keys
  db.py                  SQLite store: schema, nesting-aware transactions, typed accessors
  models.py              Dataclasses for rows and the status enums
  gitops.py              argv-only git wrappers: status, graph, branches, staging, commit,
                         apply, revert, fetch/pull/push, signature status
  indexer.py             git ls-files walk, tokenizer, inverted index, BM25 search
  diffs.py               extract / parse / validate unified diffs from untrusted text
  prompts.py             system prompt, context packing, reply parsing, manual bundle
  providers/             anthropic (SDK, streaming, adaptive thinking, refusal fallbacks),
                         local (OpenAI-compatible API over urllib), claude-cli (headless
                         Claude Code under the user's plan), manual (degraded), scripted (test only)
  runner.py              test command execution without a shell
  workflow.py            propose -> apply -> test -> commit / revert, with audit events
  exchange.py            JSON export and validated import (merge or replace)
  workspaces.py          registry of workspaces and repositories (~/.config/argonauta)
  sample.py              labelled sample records
  ui.py                  Rich console helpers with symbol + colour state markers
  logging_setup.py       rotating, redacting file logger
  web/
    app.py               `argonauta ui` lifecycle: server thread, browser launch, clean stop
    server.py            loopback ThreadingHTTPServer, token/Host/Origin checks, static files
    api.py               JSON routes for workspaces, git, assistant, credentials, terminals
    jobs.py              background jobs (one per repository at a time), polled by the page
    websocket.py         minimal RFC 6455 server side for the terminal
    terminal.py          pty sessions: spawn, resize, read/write, non-blocking shutdown
    credentials.py       read-only SSH / GPG / identity inspection
    graph.py             lane assignment for the commit graph
    static/              index.html, app.js, app.css and vendor/ (xterm.js, MIT)
```

The web layer only orchestrates: every git action goes through `gitops.py`,
every assistant action through `workflow.py`, and every record through
`db.py`, exactly as the CLI does. Each request and each job opens its own
SQLite connection.

Data model (one SQLite file per repository):

| Table | Holds |
| --- | --- |
| `meta`, `settings` | schema version, repo root, the test command |
| `files`, `terms` | index metadata (path, size, hash, language) and term counts |
| `proposals` | task, provider, model, status, explanation, diff, context files, token counts |
| `changes` | applied files, line counts, commit sha, reverted timestamp |
| `test_runs` | command, exit code, status, output tail, link to proposal and change |
| `events` | append-only audit log: assistant steps and git actions taken from the UI |

## Data location, backup, and leaving

- Per repository, everything lives in `<repo>/.argonauta/`: `argonauta.db`
  (SQLite, WAL mode), `argonauta.log`, and `prompts/` for manual-mode bundles.
  Delete the folder to remove every trace; the repository itself is untouched.
- The workspace registry lives in `~/.config/argonauta/workspaces.json` (names
  and paths only) and the model settings in `~/.config/argonauta/settings.json`
  (server URL, model name, the remote-server flag; never a key). Delete them to
  forget every workspace and setting.
- `argonauta export backup.json` writes all proposals, changes, test runs,
  events, settings and index metadata (paths and hashes, never file contents) as
  plain JSON. Diffs are included because they are your work.
- `argonauta import backup.json` merges records with fresh ids;
  `--replace` wipes the existing records first and asks for confirmation. Imports
  are validated field by field and applied atomically. Settings, including the
  test command, are never imported because `apply` executes that command; the
  CLI shows what the file contained so you can set it on purpose.
- The SQLite file can also be copied while nothing is running, or inspected
  with any SQLite tool.

## Sample data

`argonauta sample load` inserts three `[SAMPLE]`-prefixed proposals (applied,
rejected, pending with a failed test run) so you can see what history looks like
in the CLI and in the Proposals tab. `argonauta sample clear` deletes them;
sample proposals can never be applied.

`examples/sample_project/` is a tiny calculator with one deliberate bug, and
`examples/sample_reply.md` is a model-shaped reply that fixes it. `scripts/demo.sh`
copies the project to a temporary directory and runs the whole loop with the
test-only `scripted` provider, so you can watch it work without a key. To try
the same in the web UI, export `ARGONAUTA_PROVIDER=scripted` and
`ARGONAUTA_SCRIPTED_DIFF=$PWD/examples/sample_reply.md` before `argonauta ui`,
add a copy of the sample project with "+ Repo", then Initialize, Index,
Propose and Apply.

## Accessibility

Web UI: every control is a real button, link, input or select with a text
label next to its icon; panels, tabs, dialogs and the toast area carry ARIA
roles and live regions; focus rings are visible; keyboard shortcuts cover the
common actions and never fire while you type in a field or in the terminal;
colour never carries meaning alone; both the light and the dark palette are
chosen for AA contrast.

Command line: every state is marked with a symbol (`✓`, `✗`, `!`, `·`, `○`,
`↩`) as well as a colour; `--plain` and the `NO_COLOR` environment variable
turn colour and spinners off; tables carry column headers; every command and
option has a help string reachable with `-h`; confirmations state what will
happen and default to "no"; when stdin is not a terminal the CLI refuses to
guess and asks for `--yes` instead of hanging.

## Scripts

| Script | Purpose |
| --- | --- |
| `scripts/install.sh` | create `.venv`, install with extras, create `.env` |
| `scripts/dev.sh ...` | run the CLI from source (`scripts/dev.sh ui` starts the web UI) |
| `scripts/test.sh` | run unit, API and end-to-end tests |
| `scripts/build.sh` | build wheel and sdist into `dist/` (static assets included), run `pip check` |
| `scripts/run.sh ...` | production-style run: install the built wheel into `.venv-prod` and run it |
| `scripts/demo.sh` | run the full assistant loop on a throwaway copy of the sample project |

## Tests

`scripts/test.sh` runs 142 tests against real git repositories, a real server
thread and real pty shells, not mocks:

- core: store round trips and cascade deletes, diff extraction and path
  validation (traversal, absolute paths, protected files, symlinks), tokenizer
  and BM25 ranking with incremental re-indexing, reply parsing, export/import
  round trips with rejection of malformed payloads and atomicity, the workflow
  against a temporary repository (propose, apply, test, commit, revert, failed
  tests blocking commits, dirty-tree refusal, invalid diffs), log redaction,
  `.env` allowlisting, the local provider against a fake OpenAI-compatible
  server (request shape, auth header, error mapping, loopback rule), and an
  end-to-end CLI smoke test run as a subprocess;
- git operations: branches, graph, status areas, staging, discard, commit,
  branch lifecycle, fetch/pull/push against a bare remote, a remote that needs
  credentials failing fast, refs that look like flags being rejected;
- web layer: workspace registry (including concurrent writers), job runner,
  WebSocket framing (masking, size limits, fragments, ping), pty sessions,
  credential parsing, graph lanes, the JSON API against a live server (missing
  or foreign token, Origin and Host, oversized bodies, path traversal, missing
  repositories, staging, commits, branches, remote jobs, the assistant loop,
  manual mode, credentials, terminals), a WebSocket terminal round trip, the
  shell dying when the socket closes, and `argonauta ui` started as a
  subprocess and stopped with Ctrl+C.

## Limitations (on purpose)

- **Model quality is whatever model you point it at.** Argonauta does not claim
  frontier coding quality, and the default model can be changed in `.env`.
- **Context is small and lexical.** Retrieval is BM25 over identifiers, capped
  at a handful of files and tens of kilobytes. There are no embeddings, no
  repository-wide context windows and no multi-file planning.
- **No IDE integration, no background daemon.** The web UI exists only while
  `argonauta ui` runs; latency is a local server plus one model call.
- **Single user.** One person, one machine, one browser tab at a time is the
  design. There is no sync, sharing or multi-user history, and the server must
  never be exposed beyond localhost; the embedded terminal is a real shell.
- **Unified diffs only.** Replies without a parseable diff are stored as
  `invalid` with the model's explanation so you can read why.
- **Not a full Git client.** The web UI covers staging, commits, local
  branches, fetch, fast-forward pull and push. Merge, rebase, conflict
  resolution, stash, hunk-level staging, tags, submodules and remote branch
  deletion stay with `git` itself (the terminal tab is right there).
- **Terminal tabs do not survive a page reload**; the shell is closed when the
  socket closes.
- **No security certification.** Input validation and the protected-path rules
  are tested, but this is a personal tool: review every diff before `apply`.

## License

GPL-3.0-or-later, see `LICENSE`.
