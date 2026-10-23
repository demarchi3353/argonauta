/* Argonauta web UI. One file, no build step, no network beyond the local server. */
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const REPORT = window.ArgonautaReport;
  const esc = REPORT.esc;
  const LANES = ["--lane0", "--lane1", "--lane2", "--lane3", "--lane4", "--lane5"];
  const SIG = { good: "✓", bad: "✗", good_untrusted: "~", expired_signature: "⌛", expired_key: "⌛", revoked: "✗", unknown_key: "?", unsigned: "·" };
  const ic = (name) => '<svg class="ic" aria-hidden="true"><use href="#' + name + '"/></svg>';
  const REPO_BUTTONS = ["btn-fetch", "btn-pull", "btn-push", "btn-new-branch", "btn-new-branch-2", "btn-stage-all", "btn-unstage-all", "btn-commit", "btn-review-run"];

  const S = {
    token: null, workspaces: [], workspaceId: null, repoId: null, stateSeq: 0, state: null, graph: [], proposals: [], events: [],
    selection: null, openDiff: null, openReport: null, reportSeq: 0, detail: null, diffSeq: 0, graphScroll: 0, inspectorTab: "detail", bottomTab: "events", bottomOpen: false, sidePanel: null, terminals: new Map(), busy: new Set(), filter: "", branchFilter: "", pollTimer: null, changeCount: 0,
    reviews: [], prs: null, prsOrigin: null, prsError: null, ghStatus: null, ghStatusRepo: null, ghLoading: false, reviewSel: null, review: { source: "worktree", mode: "code", pr: null },
  };

  // ----- token and api -----------------------------------------------------------------
  function readToken() {
    const m = /[#&]token=([A-Za-z0-9_\-]+)/.exec(location.hash);
    if (m) { try { sessionStorage.setItem("argonauta.token", m[1]); } catch (e) { /* private mode */ } history.replaceState(null, "", location.pathname); return m[1]; }
    try { return sessionStorage.getItem("argonauta.token"); } catch (e) { return null; }
  }
  async function api(method, path, body) {
    const headers = { Authorization: "Bearer " + S.token };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) { const err = new Error((data && data.error) || ("HTTP " + resp.status)); err.status = resp.status; err.data = data; throw err; }
    return data;
  }
  const rp = (suffix) => "/api/repos/" + S.repoId + suffix;

  // ----- toasts, dialogs, busy ---------------------------------------------------------------
  function toast(kind, text, sticky) {
    const el = document.createElement("div");
    el.className = "toast " + kind; el.setAttribute("role", kind === "bad" ? "alert" : "status");
    el.innerHTML = '<span class="sym">' + ({ ok: "✓", bad: "✗", warn: "!", info: "·" }[kind] || "·") + "</span>" + esc(text);
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), sticky ? 15000 : 5000);
  }
  function dialog(title, text, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const d = $("#dialog");
      $("#dialog-title").textContent = title; $("#dialog-text").textContent = text;
      const label = $("#dialog-input-label"), input = $("#dialog-input");
      label.hidden = !opts.input; $("#dialog-input-name").textContent = opts.input || ""; input.value = opts.value || "";
      $("#dialog-ok").textContent = opts.ok || "OK"; $("#dialog-ok").className = "btn " + (opts.danger ? "danger" : "primary");
      const onClose = () => { d.removeEventListener("close", onClose); resolve(d.returnValue === "ok" ? (opts.input ? input.value : true) : null); };
      d.addEventListener("close", onClose); d.showModal();
      if (opts.input) input.focus(); else $("#dialog-ok").focus();  // Enter confirms; Esc cancels
    });
  }
  function renderRemoteButtons() {
    const anyRemote = ["fetch", "pull", "push"].some((k) => S.busy.has(k)), ahead = S.state ? S.state.ahead : 0;
    ["fetch", "pull", "push"].forEach((k) => { const b = $("#btn-" + k); b.disabled = anyRemote || !S.repoId; b.innerHTML = (S.busy.has(k) ? '<span class="spinner"></span>' : ic("i-" + k)) + (k === "push" && ahead > 0 ? "Push " + ahead + "↑" : k[0].toUpperCase() + k.slice(1)); });
    $("#btn-push").classList.toggle("primary", ahead > 0);
  }
  function setBusy(key, on) {
    if (on) S.busy.add(key); else S.busy.delete(key);
    const noRepo = !S.repoId;
    renderRemoteButtons();
    const assistantBusy = ["index", "propose", "test", "apply", "review"].some((k) => S.busy.has(k));
    ["propose", "index", "test"].forEach((k) => { const b = $("#btn-" + k); if (b) b.disabled = assistantBusy; });
    $("#btn-commit").disabled = S.busy.has("commit") || noRepo;
    renderRail(); renderReviewTab(); // the run button follows every assistant job, not only reviews
  }
  function setRepoControls(enabled, reason) {
    REPO_BUTTONS.forEach((id) => { const b = $("#" + id); if (!b) return; b.disabled = !enabled; b.title = enabled ? "" : reason; });
    $("#commit-message").disabled = !enabled; $("#commit-filter").disabled = !enabled;
  }

  // ----- jobs ------------------------------------------------------------------------------
  async function runJob(kind, body, label, errorEl) {
    setBusy(kind, true);
    const status = toastProgress(label || kind);
    try {
      const data = await api("POST", rp("/jobs/" + kind), body || {});
      let job = data.job;
      while (job.status === "queued" || job.status === "running") {
        await new Promise((r) => setTimeout(r, 500));
        job = await api("GET", "/api/jobs/" + job.id);
        status.update(job.progress || label || kind);
      }
      status.remove();
      if (job.status === "failed") { if (errorEl) errorEl.textContent = job.error; else toast("bad", (label || kind) + ": " + job.error, true); return null; } // errorEl: show it inline in the form
      return job.result;
    } catch (err) {
      status.remove();
      if (err.status === 409 && err.data && err.data.job) toast("warn", "Another operation is running: " + err.data.job.kind);
      else toast("bad", (label || kind) + ": " + err.message, true);
      return null;
    } finally { setBusy(kind, false); refreshState(); }
  }
  function toastProgress(text) {
    const el = document.createElement("div"); el.className = "toast info"; el.setAttribute("role", "status");
    el.innerHTML = '<span class="spinner"></span><span class="t"></span>'; $(".t", el).textContent = text; $("#toasts").appendChild(el);
    return { update: (t) => { $(".t", el).textContent = t; }, remove: () => el.remove() };
  }

  // ----- workspaces ---------------------------------------------------------------------------
  async function loadWorkspaces() {
    const data = await api("GET", "/api/workspaces");
    S.workspaces = data.workspaces;
    const repos = S.workspaces.flatMap((w) => w.repos.map((r) => ({ ...r, workspace: w })));
    let remembered = null; try { remembered = localStorage.getItem("argonauta.repo"); } catch (e) { /* ignore */ }
    if (!S.repoId || !repos.some((r) => r.id === S.repoId)) {
      const pick = repos.find((r) => r.id === remembered && r.exists) || repos.find((r) => r.exists) || null;
      S.repoId = pick ? pick.id : null;
    }
    renderWorkspaces();
  }
  // the switcher is a plain list: each repository name is a button, its actions are sibling buttons in the same row
  function repoButton(r, title, icon) {
    return '<button type="button" class="repo-name" data-repo="' + esc(r.id) + '" title="' + esc(title) + '"' + (r.id === S.repoId ? ' aria-current="true"' : "") + (r.exists ? "" : ' aria-disabled="true"') + ">" + ic(icon) + '<span>' + esc(r.name) + (r.exists ? "" : " (missing)") + "</span></button>";
  }
  function renderWorkspaces() {
    const tree = $("#workspace-list");
    const current = S.workspaces.find((w) => w.id === S.workspaceId) || S.workspaces.find((w) => w.repos.some((r) => r.id === S.repoId)) || S.workspaces[0];
    if (current) S.workspaceId = current.id;
    const cur = current && current.repos.find((r) => r.id === S.repoId);
    $("#repo-switch-label").innerHTML = current ? '<span class="muted">' + esc(current.name) + "</span> / " + (cur ? esc(cur.name) : '<span class="muted">no repository</span>') : "No repository";
    tree.innerHTML = S.workspaces.map((w) => '<li class="ws">' + ic("i-folder") + esc(w.name) + ' <button class="btn mini ghost" data-del-ws="' + esc(w.id) + '" aria-label="Remove workspace ' + esc(w.name) + '" title="Remove workspace">' + ic("i-x") + '</button></li>' +
      (w.repos.length ? "" : '<li class="repo muted"><em>empty: use “+ Repo” to add a repository</em></li>') +
      w.repos.filter((r) => !r.parent_id).map((r) => '<li class="repo' + (r.id === S.repoId ? " active" : "") + (r.exists ? "" : " missing") + '">' + repoButton(r, r.path, "i-repo") +
        '<button class="btn mini ghost" data-add-wt="' + esc(r.id) + '" aria-label="New worktree of ' + esc(r.name) + '" title="New worktree: a separate checkout with its own terminal">' + ic("i-plus") + ic("i-worktree") + '</button>' +
        '<button class="btn mini ghost" data-del-repo="' + esc(r.id) + '" data-ws="' + esc(w.id) + '" aria-label="Remove repository ' + esc(r.name) + '" title="Remove from workspace">' + ic("i-x") + '</button></li>' +
        w.repos.filter((c) => c.parent_id === r.id).map((c) => '<li class="repo wt' + (c.id === S.repoId ? " active" : "") + (c.exists ? "" : " missing") + '">' + repoButton(c, "Worktree at " + c.path, "i-worktree") +
          '<button class="btn mini ghost" data-del-wt="' + esc(c.name) + '" data-main="' + esc(r.id) + '" aria-label="Remove worktree ' + esc(c.name) + '" title="Remove worktree (the branch is kept)">' + ic("i-trash") + '</button></li>').join("")).join("")).join("");
    if (!S.workspaces.length) tree.innerHTML = '<li class="empty">No workspaces. Use “+ Workspace”, then “+ Repo”.</li>';
  }
  async function selectRepo(id) {
    S.repoId = id; S.selection = null; S.detail = null; S.graph = []; S.proposals = []; S.stateSeq++; closeReport(); closeDiffView();
    S.prs = null; S.prsOrigin = null; S.prsError = null; S.review.pr = null; S.reviewSel = null; S.reviews = []; $("#review-pr").dataset.html = ""; $("#review-error").textContent = "";
    const owner = S.workspaces.find((w) => w.repos.some((r) => r.id === id)); if (owner) S.workspaceId = owner.id;
    try { localStorage.setItem("argonauta.repo", id); } catch (e) { /* ignore */ }
    renderWorkspaces(); renderDetail();
    if ($("#repo-menu").open) closeRepoMenu();
    await refreshAll();
  }
  // the switcher popover: a non-modal dialog under its trigger; Escape, an outside click or a pick closes it
  let menuAnchor = null;
  function onMenuKey(ev) { if (ev.key === "Escape" && !document.querySelector("dialog[open]:not(#repo-menu)")) { ev.preventDefault(); closeRepoMenu(); } }
  // both openers toggle it themselves; a target detached by a re-render (the switcher label) is not an outside click
  function onMenuClick(ev) { if (ev.target.isConnected && !ev.target.closest('#repo-menu, [aria-controls="repo-menu"], dialog')) closeRepoMenu(); }
  // Tab out of the popover closes it; a confirm dialog opened from inside it or a re-render (relatedTarget null) does not
  function onMenuFocusOut(ev) { const to = ev.relatedTarget; if (to && to !== menuAnchor && !to.closest("#repo-menu, dialog[open]")) closeRepoMenu(false); }
  function onMenuArrow(ev) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(ev.key)) return;
    const rows = $$("#workspace-list [data-repo]"); if (!rows.length) return;
    const li = ev.target.closest("li"), cur = ev.target.closest("[data-repo]") || (li && li.querySelector("[data-repo]")), i = rows.indexOf(cur); // from a row action, move relative to its row
    const next = ev.key === "Home" ? 0 : ev.key === "End" ? rows.length - 1 : i < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, i + (ev.key === "ArrowDown" ? 1 : -1)));
    ev.preventDefault(); rows[next].focus();
  }
  function openRepoMenu(anchorEl) {
    const m = $("#repo-menu"), r = anchorEl.getBoundingClientRect();
    menuAnchor = anchorEl; renderWorkspaces();
    m.style.left = r.left + "px"; m.style.top = r.bottom + 4 + "px";
    if (!m.open) m.show();
    anchorEl.setAttribute("aria-expanded", "true");
    document.addEventListener("keydown", onMenuKey); document.addEventListener("click", onMenuClick);
    const first = m.querySelector('[data-repo][aria-current="true"]') || m.querySelector("[data-repo]"); if (first) first.focus();
  }
  function closeRepoMenu(returnFocus = true) {
    const m = $("#repo-menu"); if (m.open) m.close();
    document.removeEventListener("keydown", onMenuKey); document.removeEventListener("click", onMenuClick);
    if (menuAnchor) { menuAnchor.setAttribute("aria-expanded", "false"); if (returnFocus) menuAnchor.focus(); menuAnchor = null; }
  }

  // ----- state and graph -------------------------------------------------------------------
  async function refreshState() {
    const seq = S.stateSeq;
    if (!S.repoId) { S.state = null; renderState(); return; }
    let next = null;
    try { next = await api("GET", rp("/state")); } catch (err) { if (seq === S.stateSeq) toast("bad", "State: " + err.message); }
    if (seq !== S.stateSeq) return;  // the user switched repository meanwhile; this answer is stale
    S.state = next;
    renderState();
  }
  async function refreshGraph() {
    if (!S.repoId) { S.graph = []; renderGraph(); return; }
    try { S.graph = (await api("GET", rp("/graph?limit=300"))).commits; } catch (err) { S.graph = []; }
    renderGraph();
  }
  async function refreshAssistant() {
    if (!S.state || !S.state.store.initialized) { S.proposals = []; S.events = []; S.reviews = []; renderAssistant(); renderReviewTab(); return; }
    try {
      S.proposals = (await api("GET", rp("/proposals?limit=100"))).proposals;
      S.events = (await api("GET", rp("/events?limit=200"))).events;
    } catch (err) { S.proposals = []; S.events = []; }
    renderAssistant(); await refreshReviews();
  }
  async function refreshAll() { await refreshState(); await Promise.all([refreshGraph(), refreshAssistant()]); }

  function renderBranches(st) {
    const g = ArgonautaBranches.groupBranches(st.branches, st.remotes, S.branchFilter);
    const q = S.branchFilter.trim();
    const none = '<li class="muted">No branches match “' + esc(q) + '”.</li>';
    const cnt = (shown, total) => "· " + (shown === total ? total : shown + " of " + total);
    $("#branch-list").innerHTML = g.locals.map((b) => '<li role="option" tabindex="0" aria-selected="' + b.is_current + '" data-branch="' + esc(b.name) + '" title="' + (b.is_current ? "current branch" : "click to check out") + '">' + ic(b.is_current ? "i-check" : "i-branch") + '<span class="name">' + esc(b.name) + "</span>" +
      (b.upstream ? '<span class="badge" title="ahead/behind upstream">' + b.ahead + "↑" + b.behind + "↓" + (b.gone ? " gone" : "") + "</span>" : "") +
      (b.is_current ? "" : '<button class="btn mini ghost" data-del-branch="' + esc(b.name) + '" aria-label="Delete branch ' + esc(b.name) + '" title="Delete branch">' + ic("i-trash") + '</button>') + "</li>").join("") || (q ? none : '<li class="muted">No local branches yet.</li>');
    $("#remote-branch-list").innerHTML = g.groups.map((grp) => '<li class="group" role="presentation">' + ic("i-cloud") + esc(grp.remote) + " · " + grp.branches.length + "</li>" +
      grp.branches.map((b) => '<li role="option" tabindex="0" aria-selected="false" data-remote-branch="' + esc(b.name) + '" title="' + esc(b.name) + ' — click to check out a tracking branch">' + ic("i-branch") + '<span class="name">' + esc(b.short) + '</span><span class="badge">' + esc(b.sha) + "</span></li>").join("")).join("") ||
      (q ? none : '<li class="muted">No remote branches (' + (st.remotes.length ? esc(st.remotes.map((r) => r.name).join(", ")) : "no remotes") + ").</li>");
    $("#branch-count").textContent = cnt(g.localShown, g.localTotal);
    $("#remote-count").textContent = cnt(g.remoteShown, g.remoteTotal);
  }
  function renderState() {
    const st = S.state;
    $("#branch-pill").textContent = st ? (st.branch + (st.upstream ? " → " + st.upstream + " (" + st.ahead + "↑ " + st.behind + "↓)" : " (no upstream)")) : "no repository";
    renderRemoteButtons();
    const noRepoBox = $("#no-repo");
    if (st) {
      const term = st.terminal || { available: true, reason: null };
      const tb = document.querySelector('[data-rail="terminal"]');
      if (tb) { tb.disabled = !term.available; tb.title = term.available ? "New terminal" : term.reason; }
    }
    if (!st) {
      $("#branch-list").innerHTML = ""; $("#remote-branch-list").innerHTML = ""; $("#branch-count").textContent = ""; $("#remote-count").textContent = ""; renderChanges(null); $("#store-missing").hidden = true; $("#assistant").hidden = true;
      const ws = S.workspaces.find((w) => w.id === S.workspaceId);
      $("#no-repo-text").textContent = ws && !ws.repos.length ? 'Workspace “' + ws.name + '” has no repositories yet. Terminals, branches, commits and the assistant all belong to a repository.' : "No repository selected. Pick one in the repository switcher at the top.";
      noRepoBox.hidden = false; $("#graph-empty").hidden = true;
      setRepoControls(false, "Select or add a repository first"); renderReviewTab();
      return;
    }
    noRepoBox.hidden = true;
    setRepoControls(true, "");
    renderBranches(st);
    renderChanges(st.status);
    const store = st.store;
    $("#store-missing").hidden = store.initialized; $("#assistant").hidden = !store.initialized;
    $("#provider-line").textContent = store.initialized ? "Provider: " + store.provider + " · " + (store.indexed_files ? store.indexed_files + " files indexed" : "nothing indexed yet, click Index") + " · tests: " + (store.test_command || "not set") : "";
    if (store.provider_name === "manual" && store.initialized) $("#provider-line").textContent += " · degraded mode: no API key, proposals produce a prompt bundle";
    renderReviewTab();
    const running = st.running_job; if (running && !S.busy.has(running.kind)) watchJob(running);
  }
  function watchJob(job) { setBusy(job.kind, true); (async () => { let j = job; while (j.status === "queued" || j.status === "running") { await new Promise((r) => setTimeout(r, 700)); try { j = await api("GET", "/api/jobs/" + j.id); } catch (e) { break; } } setBusy(job.kind, false); refreshAll(); })(); }

  function renderChanges(status) {
    const staged = $("#staged-list"), unstaged = $("#unstaged-list");
    if (!status) { staged.innerHTML = ""; unstaged.innerHTML = ""; $("#changes-empty").hidden = true; S.changeCount = 0; renderRail(); return; }
    const total = status.staged.length + status.unstaged.length + status.untracked.length + status.conflicted.length;
    S.changeCount = total; renderRail(); $("#changes-empty").hidden = total > 0;
    const conflicts = $("#conflicts"); conflicts.hidden = !status.conflicted.length;
    conflicts.textContent = status.conflicted.length ? "Conflicts in: " + status.conflicted.map((e) => e.path).join(", ") + ". Resolve them with git, then stage." : "";
    const row = (e, area, code) => '<li><input type="checkbox" aria-label="' + (area === "index" ? "Unstage " : "Stage ") + esc(e.path) + '" data-toggle="' + area + '" data-path="' + esc(e.path) + '"' + (area === "index" ? " checked" : "") + '><span class="code ' + esc(code) + '">' + esc(code) + '</span><button type="button" class="path" data-diff="' + area + '" data-path="' + esc(e.path) + '" data-code="' + esc(code) + '">' + esc(e.path) + '</button><button type="button" class="btn mini ghost" data-discard="' + esc(e.path) + '" aria-label="Discard changes in ' + esc(e.path) + '" title="Discard changes">' + ic("i-undo") + '</button></li>';
    staged.innerHTML = status.staged.map((e) => row(e, "index", e.index)).join("") || '<li class="muted">Nothing staged.</li>';
    unstaged.innerHTML = status.unstaged.map((e) => row(e, "worktree", e.worktree)).concat(status.untracked.map((e) => row(e, "worktree", "?"))).join("") || '<li class="muted">No unstaged changes.</li>';
    const od = S.openDiff;
    if (od && (od.kind === "worktree" || od.kind === "index") && !(od.kind === "index" ? status.staged : status.unstaged.concat(status.untracked)).some((e) => e.path === od.path)) closeDiffView();
  }

  function renderGraph() {
    const box = $("#graph"), rows = S.filter ? S.graph.filter((c) => (c.subject + " " + c.author + " " + c.sha + " " + c.refs.join(" ")).toLowerCase().includes(S.filter)) : S.graph;
    $("#graph-empty").hidden = S.graph.length > 0 || !S.repoId; $("#graph-count").textContent = S.graph.length ? rows.length + " of " + S.graph.length : "";
    const maxLane = rows.reduce((m, c) => Math.max(m, c.lane, ...(c.parent_lanes || []), c.active.length - 1), 0);
    const laneW = 14, rowH = 30, x = (l) => 10 + l * laneW, y = (i) => i * rowH + rowH / 2;
    box.style.setProperty("--lanes-width", (x(maxLane) + 14) + "px");
    const color = (l) => "var(" + LANES[l % LANES.length] + ")";
    const paths = [];
    rows.forEach((c, i) => {
      const next = rows[i + 1];
      (c.active || []).forEach((on, col) => {
        if (!on) return;
        const merging = next && next.merged_lanes.includes(col) && next.lane !== col;
        if (col === c.lane) paths.push('<path d="M' + x(col) + "," + y(i) + " L" + x(col) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
        else if (c.parent_lanes.includes(col)) paths.push('<path d="M' + x(c.lane) + "," + y(i) + " C" + x(c.lane) + "," + (y(i) + rowH / 2) + " " + x(col) + "," + (y(i) + rowH / 2) + " " + x(col) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
        else if (merging) paths.push('<path d="M' + x(col) + "," + y(i) + " C" + x(col) + "," + (y(i) + rowH / 2) + " " + x(next.lane) + "," + (y(i) + rowH / 2) + " " + x(next.lane) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
        else paths.push('<path d="M' + x(col) + "," + y(i) + " L" + x(col) + "," + y(i + 1) + '" stroke="' + color(col) + '"/>');
      });
      paths.push('<circle cx="' + x(c.lane) + '" cy="' + y(i) + '" r="4.5" fill="' + color(c.lane) + '"/>');
    });
    const svg = '<svg width="' + (x(maxLane) + 14) + '" height="' + (rows.length * rowH) + '" fill="none" stroke-width="2" aria-hidden="true">' + paths.join("") + "</svg>";
    // roving tabindex: one Tab stop for the whole list (the selected commit, else the first); arrows move between rows
    const had = box.contains(document.activeElement) ? document.activeElement.dataset.sha : null; // a re-render keeps the focused row
    const sel = had && rows.some((c) => c.sha === had) ? had : S.selection && S.selection.type === "commit" && rows.some((c) => c.sha === S.selection.sha) ? S.selection.sha : rows.length ? rows[0].sha : null;
    box.innerHTML = svg + rows.map((c) => {
      const refs = c.refs.map((r) => '<span class="ref' + (r.startsWith("HEAD") ? " head" : "") + '">' + esc(r.replace("HEAD -> ", "")) + "</span>").join("");
      const word = sigWord(c.signature);
      return '<div class="row-c" role="option" tabindex="' + (c.sha === sel ? 0 : -1) + '" aria-selected="' + (S.selection && S.selection.type === "commit" && S.selection.sha === c.sha) + '" data-sha="' + esc(c.sha) + '"><span></span><span class="subject">' + refs + esc(c.subject) + '</span><span class="sig ' + word + '" title="signature: ' + word + '">' + (SIG[word] || "·") + '</span><span class="sha">' + esc(c.short) + "</span></div>";
    }).join("");
    if (had) { const r = box.querySelector('[tabindex="0"]'); if (r) r.focus({ preventScroll: true }); } // the focused row may be gone: fall back to the list's stop
  }
  function sigWord(code) { return { G: "good", B: "bad", U: "good_untrusted", X: "expired_signature", Y: "expired_key", R: "revoked", E: "unknown_key", N: "unsigned" }[code] || "unsigned"; }

  // ----- diff view: a file or a whole patch replaces the graph in the centre -----------------
  function diffHtml(diff) {
    if (!diff || !diff.trim()) return '<div class="empty">No differences.</div>';
    return '<div class="diff">' + diff.split("\n").map((line) => {
      let cls = "";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) cls = "meta";
      else if (line.startsWith("@@")) cls = "hunk"; else if (line.startsWith("+")) cls = "add"; else if (line.startsWith("-")) cls = "del";
      return '<div class="' + cls + '">' + (esc(line) || " ") + "</div>";
    }).join("") + "</div>";
  }
  function openDiffView(spec) {
    if (spec.kind === "review" && spec.text === undefined) { showReviewDiff(spec); return; }
    const wasOpen = !!S.openDiff;
    if (!wasOpen && !S.openReport) S.graphScroll = $("#graph-panel").scrollTop; // with a report open the graph is already hidden
    S.openDiff = spec;
    const short = spec.sha ? spec.sha.slice(0, 7) : "";
    const sub = { worktree: "working tree", index: "staged", commit: short, proposal: "Proposal #" + spec.pid, patch: spec.sha ? short : "Proposal #" + spec.pid, review: "Review #" + spec.rid }[spec.kind];
    $("#diff-title").textContent = spec.kind === "patch" ? "Whole patch" : spec.path;
    $("#diff-title").title = spec.kind === "patch" ? "" : spec.path;
    $("#diff-sub").innerHTML = (spec.status ? '<span class="code ' + esc(spec.status) + '">' + esc(spec.status) + "</span> " : "") + esc(sub);
    $("#diff-actions").innerHTML = spec.kind === "worktree" ? '<button class="btn" type="button" data-stage="' + esc(spec.path) + '">' + ic("i-plus") + "Stage</button>"
      : spec.kind === "index" ? '<button class="btn" type="button" data-unstage="' + esc(spec.path) + '">' + ic("i-undo") + "Unstage</button>" : "";
    const body = $("#diff-body"); body.innerHTML = diffHtml(spec.text); body.scrollTop = 0; body.scrollLeft = 0;
    // a finding opens its file at its line (new side); a line outside the diff stays at the top
    const hit = spec.kind === "review" && spec.line ? REPORT.newLineIndex(spec.text, spec.line) : -1;
    const row = hit >= 0 ? $(".diff", body).children[hit] : null;
    if (row) { row.classList.add("hit"); row.scrollIntoView({ block: "center" }); }
    $("#graph-area").hidden = true; $("#report-view").hidden = true; $("#diff-view").hidden = false;
    const back = $("#btn-diff-back"); back.textContent = S.openReport ? "← Review #" + S.openReport.rid : "← Commits"; back.title = S.openReport ? "Back to the review report (Esc)" : "Back to the commit graph (Esc)";
    // move focus into the view on open, and after a re-render dropped it (the Stage button is replaced)
    const ae = document.activeElement; if (!wasOpen || !ae || ae === document.body) $("#btn-diff-back").focus({ preventScroll: true });
    markCfile();
  }
  function closeDiffView() {
    S.diffSeq++; // drop any diff still loading
    if (!S.openDiff) return;
    const hadFocus = $("#diff-view").contains(document.activeElement);
    S.openDiff = null;
    $("#diff-view").hidden = true;
    $("#diff-body").innerHTML = ""; $("#diff-actions").innerHTML = "";
    if (S.openReport) { $("#report-view").hidden = false; if (hadFocus) $("#btn-report-back").focus({ preventScroll: true }); } // a diff opened over a report returns to it
    else { $("#graph-area").hidden = false; $("#graph-panel").scrollTop = S.graphScroll; if (hadFocus) $("#graph-panel").focus({ preventScroll: true }); }
    markCfile();
  }
  // the open diff came from the commit or proposal shown in Detail (a file chunk or its whole patch)
  function diffBelongsToDetail(od, dt) { return !!(od && dt && (dt.kind === "commit" ? od.sha === dt.sha : od.pid === dt.pid)); }
  // the file row in Detail whose chunk is open in the centre
  function markCfile() {
    const od = S.openDiff, dt = S.detail;
    const same = diffBelongsToDetail(od, dt) && od.kind === dt.kind;
    $$("#pane-detail [data-cfile]").forEach((b) => b.setAttribute("aria-current", String(!!same && dt.chunks[Number(b.dataset.cfile)].path === od.path)));
  }
  async function showFileDiff(area, path, status) {
    const seq = ++S.diffSeq;
    try { const d = await api("GET", rp("/diff?path=" + encodeURIComponent(path) + "&area=" + area)); if (seq === S.diffSeq) openDiffView({ kind: area, path, status, text: d.diff }); }
    catch (err) { if (seq === S.diffSeq) toast("bad", err.message); }
  }
  // staging an untracked file adds it; unstaging an added file leaves it untracked
  function movedStatus(status, target) { return target === "index" && status === "?" ? "A" : target === "worktree" && status === "A" ? "?" : status; }

  // ----- report view: a stored review replaces the graph in the centre ------------------------
  const MODE_LABELS = { code: "Code review", received: "Received review", mine: "My review" };
  async function openReport(id) {
    const seq = ++S.reportSeq, repo = S.repoId;
    let review;
    try { review = (await api("GET", rp("/reviews/" + encodeURIComponent(id)))).review; }
    catch (err) { if (seq === S.reportSeq) toast("bad", err.message); return; }
    if (seq !== S.reportSeq || repo !== S.repoId) return;
    if (S.openDiff) { S.diffSeq++; S.openDiff = null; $("#diff-view").hidden = true; $("#diff-body").innerHTML = ""; $("#diff-actions").innerHTML = ""; markCfile(); } // the graph scroll was stored when it opened
    else if (!S.openReport) S.graphScroll = $("#graph-panel").scrollTop;
    S.openReport = { rid: review.id };
    renderReport(review);
    $("#graph-area").hidden = true; $("#report-view").hidden = false;
    $("#report-scroll").scrollTop = 0;
    $("#btn-report-back").focus({ preventScroll: true });
  }
  function renderReport(review) {
    $("#report-title").textContent = "Review #" + review.id;
    const who = (review.provider || "") + (review.model ? "/" + review.model : "");
    $("#report-sub").textContent = [MODE_LABELS[review.mode] || review.mode, review.source_label || review.source_kind, who, relTime(review.created_at)].filter(Boolean).join(" · ");
    $("#report-sub").title = review.created_at || "";
    const sum = $("#report-summary");
    sum.className = "report-summary" + (review.status === "ok" ? "" : " " + review.status);
    sum.textContent = review.status === "unparsed" ? "The model ignored the format; raw reply below"
      : review.status === "failed" ? "The provider failed: " + (review.raw || "") : REPORT.summaryLine(review);
    const cards = (review.findings || []).map((f) => REPORT.findingHtml(review, f)).join("");
    let html = "";
    if (review.status === "unparsed") html = cards + '<pre class="diff report-raw">' + esc(review.raw || "") + "</pre>"; // stored notes, then the raw reply
    else if (review.status === "ok") html = cards || '<div class="empty">No findings.</div>';
    const om = review.omitted_files || [];
    if (om.length) html += '<p class="report-omitted">Not sent to the model: ' + om.map((p) => '<span class="mono">' + esc(p) + "</span>").join(", ") + "</p>";
    $("#report-body").innerHTML = html;
  }
  function closeReport() {
    S.reportSeq++; // drop any report still loading
    if (S.reviewSel !== null) { S.reviewSel = null; renderReviewHistory(); }
    if (!S.openReport) return;
    const hadFocus = $("#report-view").contains(document.activeElement) || $("#diff-view").contains(document.activeElement);
    S.openReport = null;
    if (S.openDiff) closeDiffView(); // a review file diff goes with its report
    $("#report-view").hidden = true; $("#graph-area").hidden = false;
    $("#report-body").innerHTML = ""; $("#report-summary").textContent = "";
    $("#graph-panel").scrollTop = S.graphScroll;
    if (hadFocus) $("#graph-panel").focus({ preventScroll: true });
  }
  async function showReviewDiff(spec) {
    const seq = ++S.diffSeq;
    try { const d = await api("GET", rp("/reviews/" + encodeURIComponent(spec.rid) + "/diff?path=" + encodeURIComponent(spec.path))); if (seq === S.diffSeq && S.openReport && S.openReport.rid === spec.rid) openDiffView(Object.assign({}, spec, { text: d.diff })); }
    catch (err) { if (seq === S.diffSeq) toast("bad", err.status === 404 ? spec.path + " is not in the reviewed diff" : err.message); }
  }

  // ----- right pane ----------------------------------------------------------------------
  const RTF = typeof Intl !== "undefined" && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat("en", { numeric: "auto" }) : null;
  function relTime(iso) {
    const t = Date.parse(iso); if (isNaN(t) || !RTF) return iso || "";
    const s = (t - Date.now()) / 1000;
    for (const [unit, n] of [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]]) if (Math.abs(s) >= n) return RTF.format(Math.trunc(s / n), unit);
    return RTF.format(0, "second");
  }
  // per-file rows for a combined patch; stats (path, status, insertions, deletions) fill in the counts.
  // A clean merge has an empty patch but still lists its files: those rows are disabled (no text to open).
  function patchFilesHtml(diff, stats) {
    if ((!diff || !diff.trim()) && !(stats && stats.length)) return { chunks: [], html: '<div class="empty">No changes.</div>' };
    const chunks = window.ArgonautaPatch.splitPatch(diff || "");
    const rows = window.ArgonautaPatch.mergeFileStats(chunks, stats); // stat paths are unquoted and renames resolved before matching
    const html = '<h3>Files (' + rows.length + ')</h3><ul class="cfiles">' + rows.map((r) => '<li><button type="button" class="cfile" ' + (r.chunkIndex === null ? 'disabled title="No textual diff for this file"' : 'data-cfile="' + r.chunkIndex + '" aria-current="false" title="Open the diff of ' + esc(r.path) + '"') + '><span class="code ' + esc(r.status) + '">' + esc(r.status) + '</span><span class="path">' + (r.oldPath ? esc(r.oldPath) + " → " : "") + esc(r.path) + '</span><span class="muted">' + (r.binary ? "binary" : "+" + esc(r.insertions) + " −" + esc(r.deletions)) + "</span></button></li>").join("") + "</ul>" +
      (chunks.length ? '<button type="button" class="btn mini ghost" data-cpatch="1">View whole patch</button>' : "");
    return { chunks, html };
  }
  // a different commit or proposal in Detail closes a diff opened from the previous one
  function closeStaleDetailDiff() {
    const od = S.openDiff, dt = S.detail;
    if (od && ["commit", "proposal", "patch"].includes(od.kind) && !diffBelongsToDetail(od, dt)) closeDiffView();
  }
  async function showCommit(sha) {
    S.selection = { type: "commit", sha }; selectTab("inspector", "detail"); renderGraph(); renderReviewTab();
    const pane = $("#pane-detail"); pane.innerHTML = '<div class="muted"><span class="spinner"></span>Loading commit…</div>';
    try {
      const d = await api("GET", rp("/commits/" + sha));
      if (!S.selection || S.selection.sha !== sha) return;
      const c = d.commit, sig = sigWord(c.signature), files = patchFilesHtml(d.diff, d.files);
      S.detail = { kind: "commit", sha: c.sha, diff: d.diff || "", chunks: files.chunks }; closeStaleDetailDiff();
      pane.innerHTML = '<h2 class="title">' + esc(c.subject) + "</h2><dl class='kv'><dt>author</dt><dd>" + esc(c.author) + " &lt;" + esc(c.email) + "&gt;</dd><dt>date</dt><dd title='" + esc(c.date) + "'>" + esc(relTime(c.date)) + "</dd><dt>sha</dt><dd class='mono'>" + esc(c.sha) + "</dd><dt>signature</dt><dd class='sig " + sig + "'>" + (SIG[sig] || "·") + " " + sig.replace("_", " ") + (d.signer ? " · " + esc(d.signer) : "") + (d.key_id ? " · key " + esc(d.key_id) : "") + "</dd>" + (c.refs.length ? "<dt>refs</dt><dd>" + esc(c.refs.join(", ")) + "</dd>" : "") + "</dl>" + (d.body ? "<p>" + esc(d.body).replace(/\n/g, "<br>") + "</p>" : "") + files.html;
      markCfile();
    } catch (err) { S.detail = null; closeStaleDetailDiff(); pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  async function showProposal(pid) {
    S.selection = { type: "proposal", id: pid }; selectTab("inspector", "detail"); renderReviewTab();
    const pane = $("#pane-detail"); pane.innerHTML = '<div class="muted"><span class="spinner"></span>Loading proposal…</div>';
    try {
      const d = await api("GET", rp("/proposals/" + pid)); const p = d.proposal, ch = d.change, runs = d.test_runs;
      if (!S.selection || S.selection.id !== pid) return;
      const files = p.diff && p.diff.trim() ? patchFilesHtml(p.diff, null) : { chunks: [], html: "" };
      S.detail = { kind: "proposal", pid: p.id, diff: p.diff || "", chunks: files.chunks }; closeStaleDetailDiff();
      const can = { apply: p.status === "proposed", reject: ["proposed", "invalid"].includes(p.status), revert: p.status === "applied" && ch && !ch.reverted_at, commit: p.status === "applied" && ch && !ch.commit_sha };
      pane.innerHTML = "<h2>Proposal #" + p.id + (p.is_sample ? " [SAMPLE]" : "") + "</h2><dl class='kv'><dt>status</dt><dd>" + esc(p.status) + "</dd><dt>task</dt><dd>" + esc(p.task) + "</dd><dt>provider</dt><dd>" + esc(p.provider + " / " + p.model) + "</dd><dt>context</dt><dd>" + esc(p.context_files.join(", ") || "(none)") + "</dd>" + (p.notes ? "<dt>notes</dt><dd>" + esc(p.notes) + "</dd>" : "") + (ch ? "<dt>change</dt><dd>" + ch.files.length + " files, +" + ch.insertions + " −" + ch.deletions + (ch.commit_sha ? ", commit " + esc(ch.commit_sha.slice(0, 10)) : ", not committed") + (ch.reverted_at ? ", reverted" : "") + "</dd>" : "") + "</dl>" +
        '<div class="row">' + (can.apply ? '<label class="check"><input type="checkbox" id="apply-test" checked> run tests</label><label class="check"><input type="checkbox" id="apply-commit" checked> commit on green</label><button class="btn primary" data-prop-apply="' + p.id + '">' + ic("i-check") + 'Apply</button>' : "") + (can.reject ? '<button class="btn" data-prop-reject="' + p.id + '">' + ic("i-x") + 'Reject</button>' : "") + (can.commit ? '<button class="btn" data-prop-commit="' + p.id + '">' + ic("i-commit") + 'Commit</button>' : "") + (can.revert ? '<button class="btn danger" data-prop-revert="' + p.id + '">' + ic("i-undo") + 'Revert</button>' : "") + "</div>" +
        (p.explanation ? "<p>" + esc(p.explanation).replace(/\n/g, "<br>") + "</p>" : "") + files.html +
        (runs.length ? "<h3>Test runs</h3><table class='table'><thead><tr><th>#</th><th>status</th><th>exit</th><th>finished</th></tr></thead><tbody>" + runs.map((r) => "<tr><td>" + r.id + "</td><td class='status-" + (r.status === "passed" ? "ok" : "bad") + "'>" + esc(r.status) + "</td><td>" + esc(r.exit_code) + "</td><td>" + esc(r.finished_at) + "</td></tr>").join("") + "</tbody></table>" + (runs[0].status !== "passed" && runs[0].output_tail ? "<pre class='diff'>" + esc(runs[0].output_tail) + "</pre>" : "") : "");
      markCfile();
    } catch (err) { S.detail = null; closeStaleDetailDiff(); pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  async function showCredentials() {
    selectTab("inspector", "credentials"); const pane = $("#pane-credentials");
    if (!S.repoId) { pane.innerHTML = '<div class="empty">Select a repository first.</div>'; return; }
    pane.innerHTML = '<div class="muted"><span class="spinner"></span>Inspecting credentials…</div>';
    try {
      const d = await api("GET", rp("/credentials")); const id = d.identity, ssh = d.ssh, sg = d.signing, head = d.head;
      const yn = (v) => v === true ? "<span class='status-ok'>yes</span>" : v === false ? "<span class='status-bad'>no</span>" : "<span class='muted'>unknown</span>";
      pane.innerHTML = "<h2>Git identity</h2><dl class='kv'><dt>name</dt><dd>" + esc(id.name || "unset") + "</dd><dt>email</dt><dd>" + esc(id.email || "unset") + "</dd><dt>source</dt><dd>" + esc(id.source) + "</dd></dl>" +
        "<h2>SSH</h2><dl class='kv'><dt>remote</dt><dd>" + esc(ssh.remote_url || "none") + "</dd><dt>kind</dt><dd>" + esc(ssh.kind) + "</dd>" + (ssh.kind === "ssh" ? "<dt>host alias</dt><dd>" + esc(ssh.host_alias) + " → " + esc(ssh.resolved_hostname || "?") + " as " + esc(ssh.user || "?") + "</dd><dt>agent running</dt><dd>" + yn(ssh.agent.running) + "</dd><dt>key loaded</dt><dd>" + yn(ssh.agent.loaded) + "</dd>" : "") + (ssh.kind === "https" ? "<dt>credential helper</dt><dd>" + esc(ssh.credential_helper) + "</dd>" : "") + "</dl>" +
        (ssh.identity_files.length ? "<ul class='files'>" + ssh.identity_files.map((f) => "<li><span></span><span class='code'>" + (f.exists ? "✓" : "✗") + "</span><span class='path'>" + esc(f.path) + (f.type ? " · " + esc(f.type) + " " + f.bits : "") + (f.comment ? " · " + esc(f.comment) : "") + "</span><span class='muted'>" + (f.fingerprint ? esc(f.fingerprint) : "no public key file") + (f.in_agent === true ? " · in agent" : f.in_agent === false ? " · not in agent" : "") + "</span></li>").join("") + "</ul>" : "") +
        "<h2>Commit signing</h2><dl class='kv'><dt>enabled</dt><dd>" + yn(sg.enabled) + "</dd>" + (sg.enabled ? "<dt>format</dt><dd>" + esc(sg.format) + "</dd><dt>key</dt><dd>" + esc(sg.key_id || "unset") + (sg.fingerprint ? " · " + esc(sg.fingerprint) : "") + "</dd><dt>uid</dt><dd>" + esc(sg.uid || "?") + "</dd><dt>valid</dt><dd>" + esc((sg.created || "?") + " → " + (sg.expires || "no expiry")) + "</dd><dt>status</dt><dd class='status-" + (sg.status === "ok" ? "ok" : "bad") + "'>" + esc(sg.status) + "</dd>" : "") + "</dl>" +
        "<h2>HEAD signature</h2><dl class='kv'><dt>commit</dt><dd>" + esc(head.sha || "none") + "</dd><dt>status</dt><dd class='sig " + esc(head.signature) + "'>" + (SIG[head.signature] || "·") + " " + esc(head.signature.replace("_", " ")) + (head.signer ? " · " + esc(head.signer) : "") + "</dd></dl><p class='muted'>Private keys are never read. Only public key files, agent listings and git/gpg metadata.</p>";
    } catch (err) { pane.innerHTML = '<div class="empty">✗ ' + esc(err.message) + "</div>"; }
  }
  function renderDetail() { if (!S.selection) $("#pane-detail").innerHTML = '<div class="empty">Select a commit, a proposal or a file.</div>'; }

  // ----- assistant -----------------------------------------------------------------------
  function renderAssistant() {
    const table = $("#proposals-table"), body = $("tbody", table);
    const emptyBox = $("#proposals-empty");
    if (!S.repoId) { emptyBox.textContent = "Select a repository to use the assistant."; emptyBox.hidden = false; }
    else { emptyBox.textContent = "No proposals yet. Describe a change above."; emptyBox.hidden = S.proposals.length > 0 || !(S.state && S.state.store.initialized); }
    table.hidden = !S.proposals.length;
    renderRail();
    body.innerHTML = S.proposals.map((p) => "<tr tabindex='0' data-prop='" + p.id + "'><td>" + p.id + "</td><td>" + esc(p.status) + "</td><td>" + (p.last_test ? esc(p.last_test.status) : "–") + "</td><td>" + (p.change && p.change.commit_sha ? esc(p.change.commit_sha.slice(0, 8)) : "–") + "</td><td>" + esc(p.task) + "</td></tr>").join("");
    $("tbody", $("#events-table")).innerHTML = S.events.map((e) => "<tr><td>" + esc(e.ts.slice(0, 19).replace("T", " ")) + "</td><td>" + esc(e.kind) + "</td><td>" + esc(e.message) + "</td></tr>").join("") || "<tr><td colspan='3' class='muted'>No events yet.</td></tr>";
  }

  // ----- branches -------------------------------------------------------------------------
  async function newBranch() {
    if (!S.repoId) return;
    const name = await dialog("New branch", "Created from the current HEAD and checked out.", { input: "Branch name", ok: "Create" }); if (!name) return;
    try { await api("POST", rp("/branches"), { name }); await api("POST", rp("/checkout"), { name }); toast("ok", "On " + name); await refreshAll(); } catch (e) { toast("bad", e.message, true); }
  }

  // ----- worktrees ------------------------------------------------------------------------
  async function newWorktree(mainId) {
    const main = S.workspaces.flatMap((w) => w.repos).find((r) => r.id === mainId); if (!main) return;
    const name = await dialog("New worktree of " + main.name, "A separate checkout in .worktrees/<name>, on a new branch with the same name, from the current HEAD. Open a terminal in it to run a second agent or task side by side.", { input: "Name (also the branch name)", ok: "Create" });
    if (!name) return;
    try {
      const r = await api("POST", "/api/repos/" + mainId + "/worktrees", { name: name.trim() });
      await loadWorkspaces(); toast("ok", "Worktree " + r.repo.name + " on branch " + (r.worktree && r.worktree.branch || name)); await selectRepo(r.repo.id);
    } catch (e) { toast("bad", e.message, true); }
  }
  async function removeWorktree(mainId, name) {
    if (!(await dialog("Remove worktree", "Remove worktree " + name + "? Its folder is deleted; the branch " + name + " is kept.", { ok: "Remove", danger: true }))) return;
    const url = "/api/repos/" + mainId + "/worktrees/" + encodeURIComponent(name);
    try { await api("DELETE", url); }
    catch (e) {
      if (e.status === 409 && /uncommitted/.test(e.message)) {
        if (await dialog("Uncommitted changes", "The worktree " + name + " has uncommitted changes that will be lost. Remove it anyway?", { ok: "Remove anyway", danger: true })) { try { await api("DELETE", url + "?force=1"); } catch (x) { toast("bad", x.message, true); return; } } else return;
      } else { toast("bad", e.message, true); return; }
    }
    const removed = S.workspaces.flatMap((w) => w.repos).find((r) => r.parent_id === mainId && r.name === name);
    if (removed) S.terminals.forEach((t, id) => { if (t.repoId === removed.id) closeTerminal(id); });
    if (removed && S.repoId === removed.id) S.repoId = mainId;
    await loadWorkspaces(); toast("ok", "Removed worktree " + name); await selectRepo(S.repoId);
  }

  // ----- terminals -----------------------------------------------------------------------
  function terminalName(repoId) {
    const all = S.workspaces.flatMap((w) => w.repos), r = all.find((x) => x.id === repoId); if (!r) return "";
    const parent = r.parent_id && all.find((x) => x.id === r.parent_id);
    return parent ? parent.name + " \u203a " + r.name : r.name;
  }
  function xtermTheme() {
    const cs = getComputedStyle(document.documentElement), v = (n) => cs.getPropertyValue(n).trim();
    return { background: v("--bg-0"), foreground: v("--text"), cursor: v("--accent"), selectionBackground: v("--bg-3") };
  }
  async function openTerminal() {
    if (!S.repoId) { toast("warn", "Terminals belong to a repository: select one in the repository switcher at the top, or add one to this workspace with “+ Repo”."); return; }
    if (S.state && S.state.terminal && !S.state.terminal.available) { toast("bad", S.state.terminal.reason); return; }
    let data;
    try { data = await api("POST", rp("/terminals"), { cols: 120, rows: 30 }); } catch (err) { toast("bad", "Terminal: " + err.message); return; }
    const t = data.terminal, tabs = $("#bottom-tabs");
    const tab = document.createElement("button"); tab.className = "tab"; tab.setAttribute("role", "tab"); tab.dataset.tab = "term-" + t.id; const repoName = terminalName(S.repoId);
    tab.innerHTML = ic("i-terminal") + esc(repoName || "Terminal") + " <span class='close' aria-label='Close terminal' title='Close terminal'>×</span>"; tab.title = "Terminal in " + repoName; tabs.appendChild(tab);
    const pane = document.createElement("div"); pane.id = "pane-term-" + t.id; pane.className = "pane term-pane"; pane.setAttribute("role", "tabpanel"); pane.hidden = true; $("#terminal-panes").appendChild(pane);
    const term = new Terminal({ cursorBlink: true, fontSize: 13, theme: xtermTheme() }); const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(pane);
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/terminals/" + t.id); ws.binaryType = "arraybuffer";
    const entry = { id: t.id, term, fit, ws, tab, pane, repoId: S.repoId }; S.terminals.set(t.id, entry);
    // Auth must be the first frame: fit() can fire onResize, which would otherwise arrive before it.
    ws.onopen = () => { ws.send(JSON.stringify({ type: "auth", token: S.token, cols: term.cols, rows: term.rows })); openBottom("term-" + t.id); };
    ws.onmessage = (ev) => term.write(new Uint8Array(ev.data));
    ws.onclose = () => { term.write("\r\n\x1b[2m[terminal closed]\x1b[0m\r\n"); };
    term.onData((d) => { if (ws.readyState === 1) ws.send(new TextEncoder().encode(d)); });
    term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows })); });
    $(".close", tab).addEventListener("click", (ev) => { ev.stopPropagation(); closeTerminal(t.id); });
    renderBottomStrip();
  }
  async function closeTerminal(id) {
    const e = S.terminals.get(id); if (!e) return;
    try { e.ws.close(); } catch (err) { /* ignore */ }
    try { await api("DELETE", "/api/terminals/" + id); } catch (err) { /* already gone */ }
    e.term.dispose(); e.tab.remove(); e.pane.remove(); S.terminals.delete(id); renderBottomStrip();
    if (S.terminals.size === 0) selectTab("bottom", "events");
    else if (S.bottomTab === "term-" + id) selectTab("bottom", "term-" + S.terminals.keys().next().value);
  }
  window.addEventListener("resize", () => { if (S.bottomOpen) S.terminals.forEach((e) => { if (!e.pane.hidden) e.fit.fit(); }); });

  // ----- model settings (user-level, ~/.config/argonauta/settings.json) ------------------
  function pill(id, kind, text) { const el = $("#" + id); el.className = "pill" + (kind ? " " + kind : ""); el.textContent = text; }
  function syncLocalSection() {
    const picked = $$('#provider-choice input[name="provider"]').find((r) => r.checked);
    $("#local-section").hidden = !(picked && picked.value === "local");
  }
  async function openModelDialog() {
    const d = $("#model-dialog"), err = $("#model-error"), res = $("#model-test-result");
    err.textContent = ""; res.textContent = ""; $("#model-list").innerHTML = "";
    let cur;
    try { cur = await api("GET", "/api/settings/model"); } catch (e) { toast("bad", e.message, true); return; }
    $("#model-url").value = cur.local_url || ""; $("#model-name").value = cur.local_model || ""; $("#model-remote").checked = !!cur.local_allow_remote;
    const src = cur.sources || {}, cc = cur.claude_cli || {};
    const choice = $$('#provider-choice input[name="provider"]').find((r) => r.value === (cur.provider_choice || "auto")) || $('#provider-choice input[value="auto"]');
    choice.checked = true;
    $$('#provider-choice input[name="provider"]').forEach((r) => { r.disabled = src.provider === "shell"; });
    pill("pill-auto", "", "picks: " + cur.effective_provider);
    pill("pill-anthropic", cur.anthropic_key_present ? "ok" : "", cur.anthropic_key_present ? "key present" : "no key in .env");
    pill("pill-local", cur.local_model ? "ok" : "warn", cur.local_model ? cur.local_model : "no model set");
    pill("pill-claude-cli", !cc.installed ? "bad" : cc.logged_in === true ? "ok" : cc.logged_in === false ? "warn" : "", !cc.installed ? "claude not found" : cc.logged_in === true ? "logged in" + (cc.auth_method ? " via " + cc.auth_method : "") : cc.logged_in === false ? "not logged in" : "installed");
    pill("pill-manual", "", "always available");
    const shellBound = Object.keys(src).filter((k) => src[k] === "shell");
    $("#model-status").textContent = "Active now: " + cur.provider + (shellBound.length ? ". Set in your shell, read-only here: " + shellBound.join(", ") + "." : ".");
    $("#model-url").disabled = src.local_url === "shell"; $("#model-name").disabled = src.local_model === "shell"; $("#model-remote").disabled = src.local_allow_remote === "shell";
    syncLocalSection();
    d.showModal(); choice.focus();
  }
  async function testModelConnection() {
    const res = $("#model-test-result"), btn = $("#model-test");
    res.textContent = "testing…"; btn.disabled = true;
    try {
      const r = await api("POST", "/api/settings/model/test", { local_url: $("#model-url").value.trim() || "http://127.0.0.1:11434/v1", local_allow_remote: $("#model-remote").checked });
      if (r.ok) {
        res.textContent = "✓ reachable, " + r.models.length + " model(s)";
        $("#model-list").innerHTML = r.models.map((m) => '<option value="' + esc(m) + '">').join("");
        if (!$("#model-name").value && r.models.length) $("#model-name").value = r.models[0];
      } else res.textContent = "✗ " + r.error;
    } catch (e) { res.textContent = "✗ " + e.message; } finally { btn.disabled = false; }
  }
  async function saveModelSettings(ev) {
    ev.preventDefault();
    const err = $("#model-error"); err.textContent = "";
    try {
      const picked = $$('#provider-choice input[name="provider"]').find((r) => r.checked);
      const r = await api("PUT", "/api/settings/model", { provider: picked ? picked.value : "auto", local_url: $("#model-url").value, local_model: $("#model-name").value, local_allow_remote: $("#model-remote").checked });
      $("#model-dialog").close(); toast("ok", "Model settings saved. Active provider: " + r.provider, true); await refreshState();
    } catch (e) { err.textContent = e.message; }
  }

  // ----- review tab ------------------------------------------------------------------------
  // /state carries only gh.installed (no network); the login comes from /api/github/status, fetched by loadGhStatus
  function reviewSources() {
    const rs = (S.state && S.state.store && S.state.store.review_sources) || { branch_base: null, gh: { installed: false } };
    const installed = !!(rs.gh && rs.gh.installed), known = installed && S.ghStatus && S.ghStatusRepo === S.repoId ? S.ghStatus : null;
    return { branch_base: rs.branch_base, gh: { installed, checked: !!known, logged_in: known ? known.logged_in : null, user: known ? known.user : null, error: known ? known.error : null } };
  }
  function ghText(gh) {
    return !gh.installed ? "gh is not installed" : !gh.checked ? "checking the gh login…" : gh.logged_in ? "gh: logged in as " + (gh.user || "?") : gh.error || "gh is not logged in (run gh auth login)";
  }
  function ghOk() { const g = reviewSources().gh; return !!(g && g.installed && g.logged_in); }
  function reviewModeOk(mode) { return mode !== "received" || (S.review.source === "pr" && !!S.review.pr); }
  // radiogroups: one tab stop (the checked option, else the first enabled one); arrows move and select
  function rovingTabindex(group) {
    const opts = [...group.querySelectorAll('[role="radio"]')], enabled = opts.filter((b) => !b.disabled);
    const stop = enabled.find((b) => b.getAttribute("aria-checked") === "true") || enabled[0];
    opts.forEach((b) => { b.tabIndex = b === stop ? 0 : -1; });
  }
  function radioKeys(ev) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[ev.key]; if (!step) return;
    const group = ev.currentTarget, enabled = [...group.querySelectorAll('[role="radio"]')].filter((b) => !b.disabled); if (!enabled.length) return;
    ev.preventDefault();
    const i = enabled.indexOf(document.activeElement), next = enabled[((i < 0 ? 0 : i + step) + enabled.length) % enabled.length];
    next.click(); next.focus();
  }
  function renderReviewTab() {
    const rs = reviewSources(), gh = rs.gh || {}, st = S.state, R = S.review, hasRepo = !!(st && S.repoId);
    if (S.inspectorTab === "review" && hasRepo && gh.installed && S.ghStatusRepo !== S.repoId && !S.ghLoading) loadGhStatus(false);
    const commit = S.selection && S.selection.type === "commit" ? S.selection.sha : null;
    const avail = { worktree: true, commit: !!commit, branch: !!rs.branch_base, pr: ghOk() };
    if (!avail[R.source]) R.source = "worktree";
    if (!reviewModeOk(R.mode)) R.mode = "code";
    $$("#review-source [data-source]").forEach((b) => { const s = b.dataset.source; b.disabled = !hasRepo || !avail[s]; b.setAttribute("aria-checked", String(R.source === s));
      b.title = !avail[s] ? (s === "commit" ? "Select a commit in the graph first" : s === "branch" ? (rs.branch_base ? "" : "No base branch found") : ghText(gh)) : ""; });
    let note = "";
    if (R.source === "commit") { const c = S.graph.find((x) => x.sha === commit); note = commit ? (c ? c.short + " " + c.subject : commit.slice(0, 7)) : "Select a commit in the graph."; }
    else if (R.source === "branch") note = rs.branch_base && st ? st.branch + " vs " + rs.branch_base : "No base branch (main or master) was found.";
    else if (R.source === "pr") note = S.prsError || (S.prs && !S.prs.length ? "No open pull requests." : "");
    else note = "Uncommitted changes in the working tree.";
    if (R.source !== "pr" && !ghOk()) note += (note ? " " : "") + "Pull requests: " + ghText(gh) + ".";
    $("#review-source-note").textContent = note;
    const prRow = $("#review-pr-row"), sel = $("#review-pr"); prRow.hidden = R.source !== "pr";
    if (S.prs) { const html = S.prs.map((p) => '<option value="' + esc(p.number) + '">#' + esc(p.number) + " " + esc(p.title) + " (" + esc(p.author) + ")</option>").join(""); if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; } if (!S.prs.some((p) => String(p.number) === String(R.pr))) R.pr = S.prs.length ? String(S.prs[0].number) : null; sel.value = R.pr || ""; }
    else { sel.innerHTML = ""; sel.dataset.html = ""; }
    sel.disabled = !S.prs || !S.prs.length;
    if (!reviewModeOk(R.mode)) R.mode = "code";
    $$("#review-mode [data-mode]").forEach((b) => { const m = b.dataset.mode, ok = reviewModeOk(m); b.disabled = !hasRepo || !ok; b.setAttribute("aria-checked", String(R.mode === m)); b.title = ok ? "" : "Needs a pull request"; });
    rovingTabindex($("#review-source")); rovingTabindex($("#review-mode"));
    $("#review-draft-row").hidden = R.mode !== "mine";
    $("#review-gh").textContent = !st ? "" : !gh.installed ? "gh: not installed" : ghText(gh) + (gh.logged_in && S.prsOrigin ? " · origin " + S.prsOrigin : "");
    $("#review-run-label").textContent = R.mode === "code" ? "Review" : "Evaluate";
    const bad = (R.source === "commit" && !commit) || (R.source === "pr" && !R.pr);
    const noStore = hasRepo && !st.store.initialized, othersBusy = ["index", "propose", "test", "apply"].some((k) => S.busy.has(k));
    $("#review-store-missing").hidden = !noStore;
    const run = $("#btn-review-run"); run.disabled = !hasRepo || noStore || S.busy.has("review") || othersBusy || bad;
    run.title = !hasRepo ? "Select or add a repository first" : noStore ? "Initialize the store first" : othersBusy ? "Another assistant job is running" : bad ? "Choose what to review first" : "";
    renderReviewHistory();
  }
  function renderReviewHistory() {
    const ul = $("#review-history"), cur = S.reviewSel;
    const html = S.reviews.map((r) => '<li><button type="button" class="hist-row" data-review="' + esc(r.id) + '" aria-pressed="' + (cur === r.id) + '"><span class="mono">#' + esc(r.id) + "</span><span>" + esc(r.mode) + '</span><span class="hist-src">' + esc(r.source_label || r.source_kind) + "</span><span>" + esc(r.provider || "") + '</span><span class="muted" title="' + esc(r.created_at) + '">' + esc(relTime(r.created_at)) + '</span><span class="hist-status ' + esc(r.status) + '">' + esc(r.status) + "</span></button></li>").join("");
    if (ul.dataset.html !== html) { ul.innerHTML = html; ul.dataset.html = html; }
    $("#review-empty").hidden = S.reviews.length > 0;
  }
  async function refreshReviews() {
    if (!S.repoId || !S.state || !S.state.store.initialized) { S.reviews = []; renderReviewTab(); return; }
    const seq = S.stateSeq;
    try { const r = (await api("GET", rp("/reviews?limit=50"))).reviews; if (seq !== S.stateSeq) return; S.reviews = r; } catch (err) { if (seq !== S.stateSeq) return; S.reviews = []; }
    renderReviewTab();
  }
  async function loadGhStatus(refresh) {
    const repo = S.repoId; if (!repo || (S.ghLoading && !refresh)) return;
    S.ghLoading = true;
    let status;
    try { status = await api("GET", "/api/github/status" + (refresh ? "?refresh=1" : "")); }
    catch (err) { status = { installed: true, logged_in: null, user: null, error: "could not check gh: " + err.message }; }
    finally { S.ghLoading = false; }
    if (repo !== S.repoId) return;
    S.ghStatus = status; S.ghStatusRepo = repo; renderReviewTab();
  }
  async function loadPrs() {
    if (!S.repoId) return;
    const btn = $("#btn-review-prs"), seq = S.stateSeq; btn.disabled = true; S.prsError = null;
    try { const d = await api("GET", rp("/github/prs")); if (seq !== S.stateSeq) return; S.prs = d.prs; S.prsOrigin = d.origin || null; }
    catch (err) { if (seq !== S.stateSeq) return; S.prs = []; S.prsOrigin = null; S.prsError = err.data && err.data.error ? err.data.error : err.message; }
    finally { btn.disabled = false; }
    renderReviewTab();
  }
  function selectReview(id) {
    S.reviewSel = id; renderReviewHistory();
    if (typeof openReport === "function") openReport(id);
  }
  async function runReview() {
    const R = S.review, err = $("#review-error"); err.textContent = "";
    let ref = "";
    if (R.source === "commit") ref = S.selection && S.selection.type === "commit" ? S.selection.sha : "";
    else if (R.source === "pr") ref = R.pr ? String(R.pr) : "";
    if ((R.source === "commit" || R.source === "pr") && !ref) { err.textContent = "Choose what to review first."; return; }
    const draft = R.mode === "mine" ? $("#review-draft").value : "";
    if (R.mode === "mine" && !draft.trim()) { err.textContent = "Paste your draft review first."; $("#review-draft").focus(); return; }
    const res = await runJob("review", { mode: R.mode, source: { kind: R.source, ref }, draft }, "Reviewing", err);
    await refreshReviews();
    if (!res) return;
    if (res.manual) { toast("warn", res.message || "Manual mode: no review was produced.", true); return; }
    if (res.review && res.review.status === "unparsed") toast("warn", "Review #" + res.review.id + ": the model ignored the format; the raw reply is shown.", true);
    if (res.review) selectReview(res.review.id);
  }

  // ----- tabs ------------------------------------------------------------------------------
  const INSPECTOR_TABS = ["detail", "changes", "proposals", "credentials", "review"];
  function selectTab(region, name) {
    if (region === "inspector") {
      S.inspectorTab = name;
      $$("#inspector-tabs .tab").forEach((t) => { const on = t.dataset.tab === name; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; });
      INSPECTOR_TABS.forEach((n) => { $("#pane-" + n).hidden = n !== name; });
      if (name === "review") renderReviewTab(); // first visit per repository fetches the gh login
      $$("#rail [data-rail]").forEach((b) => { if (INSPECTOR_TABS.includes(b.dataset.rail)) b.setAttribute("aria-pressed", String(b.dataset.rail === name)); });
    } else {
      S.bottomTab = name;
      $$("#bottom-tabs .tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
      $("#pane-events").hidden = name !== "events";
      S.terminals.forEach((e) => { e.pane.hidden = e.tab.dataset.tab !== name; if (!e.pane.hidden && S.bottomOpen) { e.fit.fit(); e.term.focus(); } });
    }
  }
  function renderBottomStrip() { $("#bottom-strip-label").textContent = "Terminals (" + S.terminals.size + ") · Events"; }
  function setBottomOpen(open) {
    S.bottomOpen = open; $("#bottom").classList.toggle("open", open); $("#bottom-strip").setAttribute("aria-expanded", String(open));
  }
  function openBottom(tab) { setBottomOpen(true); selectTab("bottom", tab || S.bottomTab); }
  function toggleBottom() { if (S.bottomOpen) setBottomOpen(false); else openBottom(S.bottomTab); }
  function renderRail() {
    const n = S.changeCount || 0, busy = S.busy.has("propose") || S.busy.has("apply");
    const set = (id, text) => { const b = $("#" + id); b.textContent = text; b.hidden = !text; };
    set("badge-changes", n > 99 ? "99+" : n ? String(n) : ""); set("count-changes", n ? String(n) : "");
    set("badge-proposals", busy ? "…" : ""); set("badge-review", S.busy.has("review") ? "…" : ""); set("count-proposals", S.proposals.length ? String(S.proposals.length) : "");
    // the badge is capped and decorative; the button's name carries the real count
    $('#rail [data-rail="changes"]').setAttribute("aria-label", n ? "Changes, " + n + (n === 1 ? " file" : " files") : "Changes");
    $('#rail [data-rail="proposals"]').setAttribute("aria-label", busy ? "Proposals, running" : "Proposals");
    $('#rail [data-rail="review"]').setAttribute("aria-label", S.busy.has("review") ? "Review, running" : "Review");
  }
  function toggleSidePanel(name) {
    const open = S.sidePanel !== name; S.sidePanel = open ? name : null;
    const panel = $("#side-" + name), btn = $('[data-rail="' + name + '"]');
    panel.hidden = !open; btn.setAttribute("aria-pressed", String(open));
    if (open) { const f = name === "branches" ? $("#branch-filter") : panel.querySelector("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"); if (f) f.focus(); }
    else btn.focus();
  }
  function onRail(ev) {
    const b = ev.target.closest("[data-rail]"); if (!b) return; const k = b.dataset.rail;
    if (k === "repos") { if ($("#repo-menu").open) closeRepoMenu(); else openRepoMenu(b); }
    else if (k === "credentials") showCredentials();
    else if (k === "changes" || k === "proposals" || k === "review") selectTab("inspector", k);
    else if (k === "terminal") openTerminal();
    else if (k === "events") openBottom("events");
    else if (k === "model") openModelDialog();
    else if (k === "branches") toggleSidePanel("branches");
  }
  function onInspectorKey(ev) {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return; const t = ev.target.closest(".tab"); if (!t) return;
    const i = INSPECTOR_TABS.indexOf(t.dataset.tab), name = INSPECTOR_TABS[(i + (ev.key === "ArrowRight" ? 1 : INSPECTOR_TABS.length - 1)) % INSPECTOR_TABS.length];
    ev.preventDefault(); if (name === "credentials") showCredentials(); else selectTab("inspector", name); $("#tab-" + name).focus();
  }

  // ----- event wiring -----------------------------------------------------------------------
  function wire() {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { for (const e of S.terminals.values()) e.term.options.theme = xtermTheme(); });
    $("#repo-switch").addEventListener("click", (ev) => { if ($("#repo-menu").open) closeRepoMenu(); else openRepoMenu(ev.currentTarget); });
    $("#workspace-list").addEventListener("click", async (ev) => {
      const del = ev.target.closest("[data-del-repo]"), delWs = ev.target.closest("[data-del-ws]"), addWt = ev.target.closest("[data-add-wt]"), delWt = ev.target.closest("[data-del-wt]"), repo = ev.target.closest("[data-repo]");
      if (addWt) { await newWorktree(addWt.dataset.addWt); return; }
      if (delWt) { await removeWorktree(delWt.dataset.main, delWt.dataset.delWt); return; }
      if (del) { if (await dialog("Remove repository", "Remove it from the workspace? Files on disk are not touched.", { ok: "Remove", danger: true })) { await api("DELETE", "/api/workspaces/" + del.dataset.ws + "/repos/" + del.dataset.delRepo).catch((e) => toast("bad", e.message)); await loadWorkspaces(); await refreshAll(); } return; }
      if (delWs) { if (await dialog("Remove workspace", "Remove this workspace and its repository list? Files on disk are not touched.", { ok: "Remove", danger: true })) { await api("DELETE", "/api/workspaces/" + delWs.dataset.delWs).catch((e) => toast("bad", e.message)); await loadWorkspaces(); await refreshAll(); } return; }
      if (repo && !repo.closest(".missing")) selectRepo(repo.dataset.repo);
    });
    $("#workspace-list").addEventListener("keydown", onMenuArrow); // Enter and Space are the repo buttons' own clicks
    $("#repo-menu").addEventListener("focusout", onMenuFocusOut);
    $("#btn-add-workspace").addEventListener("click", async () => { const name = await dialog("New workspace", "", { input: "Name", ok: "Create" }); if (name) { try { await api("POST", "/api/workspaces", { name }); await loadWorkspaces(); toast("ok", "Workspace created"); } catch (e) { toast("bad", e.message); } } });
    $("#btn-add-repo-2").addEventListener("click", () => $("#btn-add-repo").click());
    $("#btn-add-repo").addEventListener("click", async () => {
      if (!S.workspaces.length) { toast("warn", "Create a workspace first."); return; }
      const wid = S.workspaceId || S.workspaces[0].id;
      const path = await dialog("Add repository", "Absolute path to a folder inside a git repository on this machine.", { input: "Path", ok: "Add" });
      if (path) { try { const r = await api("POST", "/api/workspaces/" + wid + "/repos", { path }); await loadWorkspaces(); toast("ok", "Added " + r.name); selectRepo(r.id); } catch (e) { toast("bad", e.message, true); } }
    });
    ["fetch", "pull", "push"].forEach((k) => $("#btn-" + k).addEventListener("click", async () => {
      if (!S.repoId) return;
      const body = {}; if (k === "push" && S.state && !S.state.upstream) { if (!(await dialog("Push", "No upstream is set. Push to origin and set it as upstream?", { ok: "Push" }))) return; body.set_upstream = true; }
      const res = await runJob(k, body, k[0].toUpperCase() + k.slice(1)); if (res) toast("ok", k + " done" + (res.output ? ": " + res.output.split("\n").slice(-1)[0] : "")); await refreshGraph();
    }));
    $("#btn-help").addEventListener("click", () => $("#help").showModal());
    $("#model-test").addEventListener("click", testModelConnection);
    $("#model-form").addEventListener("submit", saveModelSettings);
    $("#provider-choice").addEventListener("change", syncLocalSection);
    $("#model-cancel").addEventListener("click", () => $("#model-dialog").close());
    $("#rail").addEventListener("click", onRail);
    $("#inspector-tabs").addEventListener("click", (ev) => { const t = ev.target.closest(".tab"); if (!t) return; if (t.dataset.tab === "credentials") showCredentials(); else selectTab("inspector", t.dataset.tab); });
    $("#inspector-tabs").addEventListener("keydown", onInspectorKey);
    $("#bottom-strip").addEventListener("click", toggleBottom);
    $("#bottom-tabs").addEventListener("click", (ev) => { const t = ev.target.closest(".tab"); if (t && !ev.target.classList.contains("close")) selectTab("bottom", t.dataset.tab); });
    $("#branch-list").addEventListener("click", async (ev) => {
      const del = ev.target.closest("[data-del-branch]"); const li = ev.target.closest("[data-branch]");
      if (del) { const name = del.dataset.delBranch; if (await dialog("Delete branch", "Delete local branch " + name + "?", { ok: "Delete", danger: true })) { try { await api("DELETE", rp("/branches/" + encodeURIComponent(name))); toast("ok", "Deleted " + name); } catch (e) { if (e.status === 409 && /not fully merged/.test(e.message) && await dialog("Branch not merged", e.message + " Delete anyway?", { ok: "Force delete", danger: true })) { await api("DELETE", rp("/branches/" + encodeURIComponent(name) + "?force=1")).catch((x) => toast("bad", x.message)); } else toast("bad", e.message); } await refreshAll(); } return; }
      if (li) { const name = li.dataset.branch; if (S.state && S.state.branch === name) return; try { await api("POST", rp("/checkout"), { name }); toast("ok", "Checked out " + name); await refreshAll(); } catch (e) { toast("bad", e.message, true); } }
    });
    $("#branch-list").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target.dataset.branch) ev.target.click(); });
    $("#remote-branch-list").addEventListener("click", async (ev) => { const li = ev.target.closest("[data-remote-branch]"); if (!li) return; const name = li.dataset.remoteBranch.split("/").slice(1).join("/"); if (await dialog("Checkout remote branch", "Create local branch " + name + " tracking " + li.dataset.remoteBranch + "?", { ok: "Checkout" })) { try { await api("POST", rp("/branches"), { name, from: li.dataset.remoteBranch }); await api("POST", rp("/checkout"), { name }); toast("ok", "Checked out " + name); await refreshAll(); } catch (e) { toast("bad", e.message, true); } } });
    $("#remote-branch-list").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target.dataset.remoteBranch) ev.target.click(); });
    $("#branch-filter").addEventListener("input", (ev) => { S.branchFilter = ev.target.value; if (S.state) renderBranches(S.state); });
    $("#branch-filter").addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && ev.target.value) { ev.preventDefault(); ev.stopPropagation(); ev.target.value = ""; S.branchFilter = ""; if (S.state) renderBranches(S.state); }
      else if (ev.key === "Enter") { ev.preventDefault(); const row = $("#side-branches").querySelector('li[role="option"]'); if (row) row.focus(); }
    });
    $("#btn-new-branch").addEventListener("click", newBranch);
    $("#btn-new-branch-2").addEventListener("click", newBranch);
    const changes = $("#pane-changes");
    changes.addEventListener("change", async (ev) => { const cb = ev.target.closest("[data-toggle]"); if (!cb) return; const path = cb.dataset.path, area = cb.dataset.toggle; try { await api("POST", rp(area === "index" ? "/unstage" : "/stage"), { paths: [path] }); } catch (e) { toast("bad", e.message); await refreshState(); return; } const od = S.openDiff, target = area === "index" ? "worktree" : "index"; if (od && (od.kind === "worktree" || od.kind === "index") && od.path === path) { od.kind = target; await showFileDiff(target, path, movedStatus(od.status, target)); } await refreshState(); });
    changes.addEventListener("click", async (ev) => {
      const diff = ev.target.closest("[data-diff]"); if (diff) { showFileDiff(diff.dataset.diff, diff.dataset.path, diff.dataset.code); return; }
      const discard = ev.target.closest("[data-discard]"); if (discard) { const p = discard.dataset.discard; if (await dialog("Discard changes", "Discard all changes in " + p + "? Untracked files are deleted. This cannot be undone.", { ok: "Discard", danger: true })) { try { await api("POST", rp("/discard"), { paths: [p] }); toast("ok", "Discarded " + p); } catch (e) { toast("bad", e.message); } const od = S.openDiff; if (od && (od.kind === "worktree" || od.kind === "index") && od.path === p) closeDiffView(); await refreshState(); } }
    });
    $("#btn-stage-all").addEventListener("click", async () => { const paths = $$("#unstaged-list [data-path]").map((e) => e.dataset.path); if (paths.length) { await api("POST", rp("/stage"), { paths }).catch((e) => toast("bad", e.message)); await refreshState(); } });
    $("#btn-unstage-all").addEventListener("click", async () => { const paths = $$("#staged-list [data-path]").map((e) => e.dataset.path); if (paths.length) { await api("POST", rp("/unstage"), { paths }).catch((e) => toast("bad", e.message)); await refreshState(); } });
    $("#commit-form").addEventListener("submit", async (ev) => {
      ev.preventDefault(); const msg = $("#commit-message").value, err = $("#commit-error"); err.textContent = "";
      if (!msg.trim()) { err.textContent = "Write a commit message."; $("#commit-message").focus(); return; }
      setBusy("commit", true);
      try { const r = await api("POST", rp("/commit"), { message: msg, amend: $("#commit-amend").checked }); toast("ok", "Committed " + r.sha.slice(0, 10)); $("#commit-message").value = ""; $("#commit-amend").checked = false; await refreshAll(); }
      catch (e) { err.textContent = e.message; } finally { setBusy("commit", false); }
    });
    $("#commit-filter").addEventListener("input", (ev) => { S.filter = ev.target.value.trim().toLowerCase(); renderGraph(); });
    $("#graph").addEventListener("click", (ev) => { const r = ev.target.closest("[data-sha]"); if (r) showCommit(r.dataset.sha); });
    $("#graph").addEventListener("focusin", (ev) => { const r = ev.target.closest("[data-sha]"); if (r) $$("#graph [data-sha]").forEach((e) => { e.tabIndex = e === r ? 0 : -1; }); });
    $("#graph").addEventListener("keydown", (ev) => { const r = ev.target.closest("[data-sha]"); if (!r) return; if (ev.key === "Enter") showCommit(r.dataset.sha); if (ev.key === "ArrowDown" && r.nextElementSibling) { ev.preventDefault(); r.nextElementSibling.focus(); } if (ev.key === "ArrowUp" && r.previousElementSibling && r.previousElementSibling.dataset.sha) { ev.preventDefault(); r.previousElementSibling.focus(); } });
    $("#btn-init").addEventListener("click", async () => { const cmd = await dialog("Initialize Argonauta store", "Optional: the command that runs this repository's tests, for example python3 -m pytest -q or .venv/bin/python -m pytest -q. It must be runnable from this machine. You can set it later.", { input: "Test command", ok: "Initialize" }); if (cmd === null) return; try { await api("POST", rp("/init"), { test_command: cmd }); toast("ok", "Store created in .argonauta/"); await refreshAll(); } catch (e) { toast("bad", e.message, true); } });
    $("#btn-index").addEventListener("click", async () => { const r = await runJob("index", {}, "Indexing"); if (r) toast("ok", "Indexed " + r.file_count + " files"); });
    $("#btn-test").addEventListener("click", async () => { const r = await runJob("test", {}, "Running tests"); if (r) { toast(r.test_run.status === "passed" ? "ok" : "bad", "Tests " + r.test_run.status + " (exit " + r.test_run.exit_code + ")", r.test_run.status !== "passed"); await refreshAssistant(); } });
    $("#propose-form").addEventListener("submit", async (ev) => {
      ev.preventDefault(); const task = $("#task").value, err = $("#propose-error"); err.textContent = "";
      if (!task.trim()) { err.textContent = "Describe the change you want."; $("#task").focus(); return; }
      const r = await runJob("propose", { task, dry_run: $("#dry-run").checked }, "Proposing"); if (!r) return;
      if (r.manual) { toast("warn", r.message, true); return; }
      if (r.dry_run) { toast("info", "Would send " + r.context.length + " files: " + r.context.join(", "), true); return; }
      $("#task").value = ""; await refreshAssistant(); showProposal(r.proposal.id);
      toast(r.proposal.status === "invalid" ? "bad" : "ok", "Proposal #" + r.proposal.id + " " + r.proposal.status + (r.proposal.notes ? ": " + r.proposal.notes : ""), r.proposal.status === "invalid");
    });
    $("#review-source").addEventListener("click", (ev) => { const b = ev.target.closest("[data-source]"); if (!b || b.disabled) return; S.review.source = b.dataset.source; if (!reviewModeOk(S.review.mode)) S.review.mode = "code"; renderReviewTab(); if (b.dataset.source === "pr" && S.prs === null) loadPrs(); });
    $("#review-mode").addEventListener("click", (ev) => { const b = ev.target.closest("[data-mode]"); if (!b || b.disabled) return; S.review.mode = b.dataset.mode; renderReviewTab(); });
    $("#review-source").addEventListener("keydown", radioKeys); $("#review-mode").addEventListener("keydown", radioKeys);
    $("#review-pr").addEventListener("change", (ev) => { S.review.pr = ev.target.value || null; renderReviewTab(); });
    $("#btn-review-prs").addEventListener("click", async () => { await loadGhStatus(true); if (ghOk()) loadPrs(); else { S.prs = null; renderReviewTab(); } });
    $("#btn-review-init").addEventListener("click", () => { selectTab("inspector", "proposals"); $("#btn-init").focus(); });
    $("#btn-review-run").addEventListener("click", runReview);
    $("#review-history").addEventListener("click", (ev) => { const b = ev.target.closest("[data-review]"); if (b) selectReview(Number(b.dataset.review)); });
    $("#proposals-table").addEventListener("click", (ev) => { const tr = ev.target.closest("[data-prop]"); if (tr) showProposal(Number(tr.dataset.prop)); });
    $("#proposals-table").addEventListener("keydown", (ev) => { const tr = ev.target.closest("[data-prop]"); if (tr && ev.key === "Enter") showProposal(Number(tr.dataset.prop)); });
    $("#pane-detail").addEventListener("click", async (ev) => {
      const b = ev.target.closest("button"); if (!b) return;
      const dt = S.detail, src = dt ? (dt.kind === "commit" ? { sha: dt.sha } : { pid: dt.pid }) : null;
      if (b.dataset.cfile && dt) { const ch = dt.chunks[Number(b.dataset.cfile)]; if (ch) openDiffView(Object.assign({ kind: dt.kind, path: ch.path, status: ch.status, text: ch.text }, src)); return; }
      if (b.hasAttribute("data-cpatch") && dt) { openDiffView(Object.assign({ kind: "patch", text: dt.diff }, src)); return; }
      if (b.dataset.propApply) { const pid = Number(b.dataset.propApply); const r = await runJob("apply", { proposal_id: pid, test: $("#apply-test").checked, commit: $("#apply-commit").checked }, "Applying #" + pid); if (r) { if (r.test_run && r.test_run.status !== "passed") toast("bad", "Tests " + r.test_run.status + "; nothing committed. Fix and commit, or revert.", true); else toast("ok", "Applied" + (r.commit_sha ? " and committed " + r.commit_sha.slice(0, 10) : "")); } await refreshAll(); showProposal(pid); return; }
      const act = b.dataset.propReject ? "reject" : b.dataset.propRevert ? "revert" : b.dataset.propCommit ? "commit" : null; if (!act) return;
      const pid = Number(b.dataset.propReject || b.dataset.propRevert || b.dataset.propCommit);
      if (act === "revert" && !(await dialog("Revert proposal", "Undo proposal #" + pid + " (git revert if committed, reverse-apply otherwise)?", { ok: "Revert", danger: true }))) return;
      try { await api("POST", rp("/proposals/" + pid + "/" + act), {}); toast("ok", act + " done"); } catch (e) { toast("bad", e.message, true); }
      await refreshAll(); showProposal(pid);
    });
    $("#btn-diff-back").addEventListener("click", () => closeDiffView());
    $("#btn-report-back").addEventListener("click", () => closeReport());
    $("#report-body").addEventListener("click", (ev) => {
      const card = ev.target.closest("[data-rpath]"); if (!card || !S.openReport) return;
      if (!ev.target.closest("button") && !window.getSelection().isCollapsed) return; // selecting text in a card is not a click
      const art = card.closest(".finding"), line = art && art.dataset.rline ? Number(art.dataset.rline) : null;
      openDiffView({ kind: "review", rid: S.openReport.rid, path: card.dataset.rpath, status: "", line });
    });
    $("#diff-actions").addEventListener("click", async (ev) => {
      const b = ev.target.closest("[data-stage], [data-unstage]"); if (!b) return;
      const stage = b.hasAttribute("data-stage"), path = stage ? b.dataset.stage : b.dataset.unstage; b.disabled = true;
      try { await api("POST", rp(stage ? "/stage" : "/unstage"), { paths: [path] }); } catch (e) { toast("bad", e.message); b.disabled = false; return; }
      const target = stage ? "index" : "worktree", od = S.openDiff, status = od ? movedStatus(od.status, target) : undefined;
      if (od && od.path === path) od.kind = target; // a poll during the re-fetch must not close the view
      await showFileDiff(target, path, status); await refreshState();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.target.closest(".xterm") || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      // Escape leaves the diff view, the report or the branches panel even from a text field (the filter, the commit message)
      if (ev.key === "Escape") { if (document.querySelector("dialog[open]")) return; if (S.openDiff) { ev.preventDefault(); closeDiffView(); } else if (S.openReport) { ev.preventDefault(); closeReport(); } else if (S.sidePanel) { ev.preventDefault(); toggleSidePanel(S.sidePanel); } return; }
      const tag = (ev.target.tagName || "").toLowerCase(); if (["input", "textarea", "select"].includes(tag)) return;
      const num = ["1", "2", "3", "4", "5"].indexOf(ev.key); if (num >= 0) { ev.preventDefault(); if (ev.key === "4") showCredentials(); else selectTab("inspector", INSPECTOR_TABS[num]); return; }
      if (ev.key === "r") { ev.preventDefault(); selectTab("inspector", "review"); renderReviewTab(); const want = S.review.mode === "mine" ? $("#review-draft") : $("#btn-review-run"); const target = !want.disabled ? want : $("#pane-review").querySelector('button:not([disabled]):not([tabindex="-1"]), select:not([disabled]), textarea:not([disabled])'); if (target) target.focus(); return; }
      if (ev.key === "p") { ev.preventDefault(); selectTab("inspector", "proposals"); $("#task").focus(); } if (ev.key === "c") { ev.preventDefault(); selectTab("inspector", "changes"); $("#commit-message").focus(); } if (ev.key === "t") { ev.preventDefault(); openTerminal(); } if (ev.key === "/") { ev.preventDefault(); $("#commit-filter").focus(); } if (ev.key === "b") { ev.preventDefault(); toggleSidePanel("branches"); } if (ev.key === "?") { ev.preventDefault(); $("#help").showModal(); }
    });
    document.addEventListener("visibilitychange", () => { if (document.hidden) clearInterval(S.pollTimer); else startPolling(); });
  }
  function startPolling() { clearInterval(S.pollTimer); S.pollTimer = setInterval(() => { if (S.repoId && !document.hidden) refreshState(); }, 5000); }

  // ----- boot ------------------------------------------------------------------------------
  async function boot() {
    S.token = readToken();
    if (!S.token) { document.body.innerHTML = '<main class="pane"><h1>Argonauta</h1><p class="empty">This page needs the one-time link printed by <code>argonauta ui</code>. Copy that URL (with its #token) into this tab.</p></main>'; return; }
    wire(); selectTab("inspector", S.inspectorTab); // rail aria-pressed and tab markup agree from the start
    try { await loadWorkspaces(); } catch (err) { toast("bad", err.status === 401 ? "The token is not valid for this server. Re-open the URL printed by argonauta ui." : err.message, true); return; }
    await refreshAll(); startPolling();
  }
  boot();
})();
