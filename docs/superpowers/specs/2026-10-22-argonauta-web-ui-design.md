# Argonauta web UI design (2026-10-22)

Follow-up to `2026-10-22-argonauta-design.md`. The CLI and core loop stay as
they are; this spec adds a local, single-user web interface that makes
Argonauta feel like GitKraken: a commit graph, staging and commits, branches
and remotes, workspaces of repositories, an embedded terminal per repository,
a credentials panel, and the assistant loop (propose, apply, test, commit,
revert) on the same screen.

## Intent and success criteria

A user runs `argonauta ui`, the browser opens, and from one page they can:

1. switch between repositories grouped in workspaces;
2. see branches, the commit graph with lanes and refs, changed files and diffs;
3. stage, unstage, discard, write a message and commit;
4. create, switch and delete local branches; fetch, pull, push;
5. open one or more terminal tabs running a real shell inside the repository;
6. read which SSH identity, GPG signing key and git identity are in effect, and
   whether each commit's signature verifies;
7. run the assistant loop (index, propose, review, apply with tests, commit,
   reject, revert) and read its history;
8. see every long operation as a loading state with progress and a clear error.

Everything stays local and private: the server binds to 127.0.0.1, serves no
external resources, and exists only while the command runs.

## Constraints

- Stack stays Python 3.12 + Typer + SQLite. The HTTP server is
  `http.server.ThreadingHTTPServer` from the standard library; the WebSocket
  endpoint is a minimal RFC 6455 implementation inside the project. No new
  runtime dependency.
- The page is one HTML file plus vanilla JavaScript and CSS shipped inside the
  package. The only third-party front-end code is a vendored copy of
  xterm.js and its fit addon (MIT), stored under
  `argonauta/web/static/vendor/` with version and license files. Nothing is
  loaded from a CDN.
- No analytics, telemetry or accounts. Secrets stay in `.env`; the credentials
  panel shows metadata only and never reads private key material.
- Excluded on purpose (documented in README limitations): merge, rebase,
  conflict resolution, stash, hunk-level staging, tags and submodule
  management, remote branch deletion, interactive prompts for credentials.

## Architecture

```
argonauta/
  web/
    __init__.py
    server.py        ThreadingHTTPServer, request router, auth, static files
    api.py           JSON handlers for git, assistant, workspaces, credentials
    jobs.py          background job runner (thread per job, polling API)
    websocket.py     RFC 6455 handshake, frame encode/decode, ping/pong, close
    terminal.py      pty sessions: spawn shell, resize, read/write, cleanup
    credentials.py   SSH / GPG / identity inspection (read-only)
    static/
      index.html, app.js, app.css
      vendor/xterm.js, xterm.css, addon-fit.js, VERSION, LICENSE
  workspaces.py      registry at ~/.config/argonauta/workspaces.json
  gitops.py          extended: branches, graph, file diffs, stage/unstage/
                     discard, checkout, create/delete branch, fetch/pull/push,
                     commit details, signature status
  cli.py             new commands: `ui`, `workspace add|remove|list`
```

The CLI remains the only owner of process lifecycle: `argonauta ui` starts the
server, prints the URL, opens the browser (unless `--no-open`), and shuts
down on Ctrl+C, killing every terminal session and cancelling jobs.

### Repositories and the registry

`~/.config/argonauta/workspaces.json` (override with `ARGONAUTA_CONFIG_DIR`):

```json
{
  "version": 1,
  "workspaces": [
    {"id": "w_8f3a", "name": "Personal", "repos": [
      {"id": "r_1c2d", "path": "/Users/me/code/argonauta", "name": "argonauta"}
    ]}
  ]
}
```

Ids are random and stable. The API only ever receives repo ids; the server
resolves them through the registry, verifies the path is still a git toplevel,
and refuses anything else. Adding a repository goes through
`POST /api/workspaces/{id}/repos` with a path the server validates with
`git rev-parse --show-toplevel`, or through `argonauta workspace add PATH`.
When `argonauta ui` runs inside a repository that is not registered, it is
added to a default workspace named "Default" so the first run is useful.

Each repository keeps its own `.argonauta/` store exactly as today. The UI
shows an "Initialize" action for repositories without one; git views work
without a store, assistant views require it.

### Request flow and security

- Bind 127.0.0.1 only; port chosen by the OS unless `--port`.
- A 32-byte random token is generated per run. The browser receives it once in
  the URL fragment (`/#token=...`), stores it in `sessionStorage`, and sends it
  as `Authorization: Bearer <token>` on every API call and as the first
  message on every WebSocket. The fragment never reaches the server log.
- Every request must carry `Host` of `127.0.0.1:<port>` or
  `localhost:<port>`; API and WebSocket requests must carry an `Origin` that
  matches. Anything else gets 403.
- Static files are served from an allowlist of known names, never from a path
  computed from the request.
- JSON bodies are size-limited (1 MB) and validated field by field. File paths
  go through the same `_path_problem` rules as diffs (relative, no `..`, not
  under `.git/`, `.argonauta/` or `.env*`).
- git is always invoked as argv, with `GIT_TERMINAL_PROMPT=0` and
  `GIT_SSH_COMMAND` untouched, so push and pull use the user's agent and fail
  fast instead of prompting.
- Terminal sessions are the most sensitive feature: a real shell over the local
  server. They inherit the token, Host and Origin checks, bind only to
  127.0.0.1, die on disconnect, and the README says so in its own section.
- Logging: ids, paths, counts, status words. Never request bodies, diffs, file
  contents, terminal traffic or tokens.

### Jobs

Long operations run in a background thread and are polled:

- `POST /api/repos/{id}/jobs/{kind}` with kind in `index`, `propose`, `test`,
  `apply`, `fetch`, `pull`, `push` returns `{job_id}` immediately.
- `GET /api/jobs/{job_id}` returns `{status: queued|running|done|failed,
  progress: "text", result: {...} | null, error: "text" | null}`.
- Jobs are kept in memory for the process lifetime, capped at 200 (oldest
  dropped). At most one job per repository runs at a time; a second request
  returns 409 with the running job id.
- Progress strings reuse the `on_progress` callbacks that already exist in the
  workflow.

### API (JSON, all under the token)

Workspaces
- `GET /api/workspaces` → registry with per-repo `initialized` and `exists`.
- `POST /api/workspaces` `{name}`; `DELETE /api/workspaces/{id}`;
  `PATCH /api/workspaces/{id}` `{name}`.
- `POST /api/workspaces/{id}/repos` `{path}`; `DELETE /api/workspaces/{id}/repos/{repo_id}`.

Repository state
- `GET /api/repos/{id}/state` → branch, head, upstream, ahead/behind, remotes,
  branches (local and remote, with upstream and last commit), status entries
  split into staged / unstaged / untracked / conflicted, store initialized,
  provider description, test command.
- `GET /api/repos/{id}/graph?limit=300` → commits `{sha, short, parents, refs,
  author, email, date, subject, signature}` in `git log --all --topo-order`
  order. The front end computes lanes.
- `GET /api/repos/{id}/commits/{sha}` → details plus per-file stats and the
  full diff; `signature` includes status, key id and signer when signed.
- `GET /api/repos/{id}/diff?path=&area=worktree|index` → unified diff for one
  file (untracked files are shown as all-added).

Git actions (synchronous; each records an event when a store exists)
- `POST .../stage` `{paths}`, `.../unstage` `{paths}`,
  `.../discard` `{paths}` (UI confirms first; untracked files are deleted).
- `POST .../commit` `{message, amend: false}` → `{sha}`. Message 1..5000 chars.
- `POST .../branches` `{name, from?}`; `POST .../checkout` `{name}`;
  `DELETE .../branches/{name}` (local only, refuses the current branch and
  unmerged branches unless `force`).
- `fetch`, `pull`, `push` are jobs (see above); `push` accepts `{set_upstream}`.

Assistant (mirrors the CLI; `propose`, `apply`, `test`, `index` are jobs)
- `POST .../init` `{test_command?}`.
- `GET .../proposals`, `GET .../proposals/{pid}` (with change, test runs).
- `POST .../proposals/{pid}/reject` `{reason?}`, `.../revert`, `.../commit`.
- `GET .../events?limit=`.
- `POST .../jobs/propose` `{task, dry_run}`; `POST .../jobs/apply`
  `{proposal_id, test, commit}`; `POST .../jobs/test` `{proposal_id?}`.
- `GET/PUT .../settings` for the test command (validated with `parse_command`).

Credentials (read-only)
- `GET /api/repos/{id}/credentials` → see below.

Terminal
- `POST /api/repos/{id}/terminals` → `{terminal_id}`; spawns `$SHELL` (or
  `/bin/zsh`, `/bin/bash`) as a login shell in a pty with cwd at the repo root
  and `TERM=xterm-256color`.
- `WS /ws/terminals/{terminal_id}`: first client message is
  `{"type":"auth","token":...,"cols":..,"rows":..}`; afterwards binary frames
  carry raw bytes both ways and text frames carry `{"type":"resize",cols,rows}`.
  Server closes the socket when the shell exits; the client closing the socket
  kills the shell. `DELETE /api/terminals/{terminal_id}` also kills it.

### Credentials panel

All values come from `git config --show-origin`, `ssh -G`, `ssh-keygen -l`,
`ssh-add -l`, `gpg --list-keys --with-colons` and `git verify-commit`.
Private key files are never opened; only `.pub` files and agent listings are
read.

```
identity:  { name, email, source: "repo .git/config" | "~/.gitconfig" | "unset" }
ssh:       { remote_url, host_alias, resolved_hostname, user,
             identity_files: [{path, exists, type, bits, fingerprint, comment}],
             agent: { running, loaded: true|false|null },
             kind: "ssh" | "https" | "none", credential_helper }
signing:   { enabled, format: "openpgp"|"ssh"|null, key_id, fingerprint,
             uid, created, expires, status: "ok"|"expired"|"missing"|"disabled" }
head:      { sha, signature: "good"|"bad"|"unsigned"|"unknown_key"|"expired", signer }
```

The graph shows a small badge per commit from `%G?` (G good, B bad, U/X/Y/R
variants, E unknown key, N unsigned), with the full text on hover and in the
commit detail pane.

### Front end

Single page, no build step. Layout (desktop first, usable at 1024px; below
that the columns stack):

```
┌ top bar: workspace ▾ · repo ▾ · branch · [Fetch] [Pull] [Push] · [Terminal] [Credentials] ┐
├ left ─────────┬ center ──────────────────────────┬ right ───────────────────────────────┤
│ WORKSPACES    │ commit graph (SVG lanes + rows)  │ diff / commit detail / proposal /     │
│ BRANCHES      │                                  │ credentials panel (tabbed)           │
│ REMOTES       │                                  │                                      │
│ CHANGES       │                                  │                                      │
│  ☐ staged     │                                  │                                      │
│  ☐ unstaged   │                                  │                                      │
│  message box  │                                  │                                      │
│  [Commit]     │                                  │                                      │
├ bottom ───────┴──────────────────────────────────┴──────────────────────────────────────┤
│ tabs: Proposals · Events · Terminal 1 · Terminal 2 · +                                  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

Behaviour:
- State refreshes after every action and every 5 s while the tab is visible.
- Every action has empty, loading, validation, success and failure states:
  empty panels name the next step; buttons disable and show a spinner while a
  job runs; validation errors appear next to the field; success and failure
  show a toast with a symbol and the message, and failures keep the text
  selectable for copying.
- Confirmation dialogs for discard, branch delete, replace-import and revert.
- Keyboard: every control is focusable in a sensible order; visible focus
  rings; shortcuts `p` propose, `c` focus commit message, `t` new terminal,
  `/` filter commits, `?` help; `Esc` closes dialogs. Lists use roving
  tabindex. ARIA roles and labels on panels, tabs, dialogs and live regions.
- Theme follows `prefers-color-scheme`; both themes meet WCAG AA contrast;
  status never relies on colour alone (symbols and text).
- No external fonts or scripts; xterm.js is served from the package.
- Implementation note: lane assignment runs server-side in `web/graph.py` so
  it is unit-tested in Python; the page only draws. Same-origin `fetch()`
  GETs carry no `Origin` header, so the API rejects a foreign Origin and
  allows an absent one; WebSocket upgrades must present it.

### Events

Every git action taken from the UI records an event in the repository store
when it exists (`git.stage`, `git.commit`, `git.checkout`, `git.push`, ...),
with paths and counts only, so `argonauta history --events` and the Events tab
show one timeline.

### CLI additions

- `argonauta ui [--port N] [--no-open] [--host 127.0.0.1]` (host is fixed to
  loopback addresses; anything else is rejected).
- `argonauta workspace list | add PATH [--workspace NAME] | remove PATH`.

### Testing

- Unit: workspace registry (create, add, remove, validation of paths and ids);
  WebSocket frame encode/decode and handshake; credentials parsing from canned
  `ssh -G`, `ssh-add -l`, `gpg --with-colons` and `git config --show-origin`
  output; lane computation for the graph (pure function, also in JS, tested
  through the Python reference); gitops additions against a temp repo.
- API: start the server in a thread on a random port against a temp
  repository with a bare local remote; exercise state, graph, stage, commit,
  branch create/checkout/delete, fetch/push jobs, propose with the scripted
  provider, apply, credentials, and auth failures (missing token, bad Origin,
  bad Host, unknown repo id, path traversal).
- Terminal: open a pty session over the WebSocket, send `echo ok\r`, assert
  the output arrives, close, assert the process is gone.
- Smoke: run `argonauta ui --no-open --port 0` as a subprocess, parse the URL
  from stdout, hit `/` and `/api/workspaces`, stop it with SIGINT and assert a
  clean exit.
- After implementation, open the page in the user's Chrome and verify the
  layout, both themes, keyboard navigation and a full propose/apply cycle.

### README changes

New sections: "Web UI" (start, layout, shortcuts), "Workspaces" (registry
location and format), "Credentials panel" (what is read, what is never read),
"Embedded terminal" (the security model, in plain words), updated
permissions, data location (`~/.config/argonauta/`), accessibility and
limitations. `scripts/dev.sh ui` and `scripts/run.sh ui` work unchanged.
