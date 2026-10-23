import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "argonauta" / "web" / "static"
DYNAMIC_IDS = {"apply-test", "apply-commit"}  # created by app.js at runtime
# XML namespaces and loopback placeholders are not network references
ALLOWED_URL_PREFIXES = ("http://127.0.0.1", "http://localhost", "http://www.w3.org/")


def test_ids_bound_in_app_js_exist_in_index_html():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html_ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
    # `$("#id")`, `$$("#id …")`; a trailing `-` (as in `"#btn-" + k`) is not an id
    bound = set(re.findall(r"""\$\$?\(\s*['"]#([A-Za-z0-9_-]*[A-Za-z0-9])(?=['"\s\[])""", js))
    missing = sorted(bound - html_ids - DYNAMIC_IDS)
    assert not missing, f"app.js binds ids missing from index.html: {missing}"


def test_index_has_brand_and_no_external_urls():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "Argonauta" in html
    urls = re.findall(r"https?://[^\s\"'<>]+", html + css)
    external = [u for u in urls if not u.startswith(ALLOWED_URL_PREFIXES)]
    assert not external, f"external network references in static UI: {external}"


def test_css_declares_slate_tokens_in_both_schemes():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for token, dark in (("--bg-0", "#0f172a"), ("--bg-1", "#131c30"), ("--bg-2", "#1b2336"), ("--bg-3", "#272f42"),
                        ("--line", "#2b3650"), ("--text", "#f1f5f9"), ("--muted", "#94a3b8"), ("--accent", "#22c55e"),
                        ("--warn", "#f97316"), ("--bad", "#ef4444"), ("--focus", "#fbbf24")):
        assert f"{token}: {dark}" in css, token
    assert "prefers-color-scheme: light" in css
    assert "--bg-0: #f8fafc" in css and "--accent: #15803d" in css
    assert '"IBM Plex Sans"' in css and '"JetBrains Mono"' in css
    assert "var(--font-mono)" not in css.split("--font-mono:", 1)[1].split(";", 1)[0]
    assert "--sel: rgba(34,197,94,.12)" in css and "--sel: rgba(22,163,74,.12)" in css
    assert "prefers-reduced-motion: reduce" in css


def test_topbar_structure():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="repo-switch"' in html and 'aria-haspopup="dialog"' in html
    assert 'id="repo-menu"' in html and 'id="workspace-list"' in html
    assert 'id="workspace-select"' not in html and 'id="repo-select"' not in html
    assert 'class="logo" src="/logo.png" alt="" width="34" height="34"' in html
    assert 'id="btn-help"' in html and 'aria-label="Help"' in html
    # the filter lives in the topbar, between the brand and the git actions
    top = html.split('<header', 1)[1].split('</header>', 1)[0]
    assert 'id="commit-filter"' in top and 'id="btn-push"' in top


def test_rail_and_inspector_structure():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for key in ("repos", "branches", "changes", "proposals", "terminal", "events", "model", "credentials"):
        assert f'data-rail="{key}"' in html, key
    assert 'id="badge-changes"' in html and 'id="badge-proposals"' in html
    for tab in ("detail", "changes", "proposals", "credentials"):
        assert f'data-tab="{tab}"' in html and f'id="pane-{tab}"' in html, tab
    assert 'class="left"' not in html
    changes = html.split('id="pane-changes"', 1)[1].split('id="pane-proposals"', 1)[0]
    assert 'id="commit-form"' in changes and 'id="staged-list"' in changes
    proposals = html.split('id="pane-proposals"', 1)[1].split('id="pane-credentials"', 1)[0]
    assert 'id="propose-form"' in proposals and 'id="proposals-table"' in proposals


def test_shortcuts_ignore_text_fields():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    guard = js.split('document.addEventListener("keydown"', 1)[1].split('"visibilitychange"', 1)[0]
    assert '"input", "textarea", "select"' in guard and ".xterm" in guard
    for key in ('"1"', '"2"', '"3"', '"4"'):
        assert key in guard, key


def test_bottom_panel_lives_under_the_graph():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    center = html.split('id="center"', 1)[1].split('class="inspector"', 1)[0]
    assert 'id="bottom"' in center and 'id="bottom-strip"' in center and 'aria-expanded' in center
    assert 'id="terminal-panes"' in center and 'id="pane-events"' in center
    assert 'data-tab="proposals"' not in center


def test_branches_side_panel():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    side = html.split('id="side-branches"', 1)[1].split('id="center"', 1)[0]
    assert 'id="branch-list"' in side and 'id="remote-branch-list"' in side and 'id="btn-new-branch"' in side
    assert 'id="side-branches" class="side" hidden' in html and 'class="box"' not in side
    assert 'data-rail="branches" aria-label' in html and 'aria-pressed="true" data-rail="branches"' not in html
    assert "function toggleSidePanel" in js and "sidePanel: null" in js


def test_diff_view_markup_and_handlers():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    center = html.split('id="center"', 1)[1].split('class="inspector"', 1)[0]
    for i in ("diff-view", "btn-diff-back", "diff-title", "diff-sub", "diff-actions", "diff-body"):
        assert f'id="{i}"' in center, i
    assert "ArgonautaPatch.splitPatch" in js
    assert "function openDiffView" in js and "function closeDiffView" in js
    # repo switch and discard close the view
    assert js.count("closeDiffView()") >= 3
    assert "No changes." in js
    # a clean merge has an empty patch but still lists its files
    assert "&& !(stats && stats.length)) return" in js


def test_copy_and_help_updated():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    readme = (STATIC.parent.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "sidebar" not in js and "sidebar" not in html.lower().split("<dialog", 1)[0]
    assert "<dt>1–4</dt>" in html or "<dt>1-4</dt>" in html
    assert 'class="logo-hero" src="/logo.png" alt="" width="96" height="96"' in html
    assert "inspector" in readme.lower() and "rail" in readme.lower()
    web_ui = readme.split("## Web UI", 1)[1].split("## Command line", 1)[0]
    assert "sidebar" not in web_ui.lower()


def test_icon_only_controls_have_labels():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for m in re.finditer(r'<button[^>]*class="[^"]*\bicon\b[^"]*"[^>]*>', html):
        assert 'aria-label="' in m.group(0), m.group(0)
    unlabeled = re.findall(r'<svg class="ic[^"]*"(?![^>]*aria-hidden="true")[^>]*>', html)
    assert not unlabeled, unlabeled  # every inline icon in the markup is decorative and hidden
    assert 'role="tablist"' in html and 'class="skip"' in html


def test_accessibility_pass_fixes():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for key in ("branches", "changes", "proposals", "credentials"):  # rail toggles start unpressed
        assert f'aria-pressed="false" data-rail="{key}"' in html, key
    # the switcher is a plain list: repo names are buttons, row actions are sibling buttons
    assert 'role="menu"' not in html and "menuitem" not in js and 'role="tree"' not in html and "treeitem" not in js
    assert '<button type="button" class="repo-name" data-repo="' in js and 'aria-current="true"' in js
    assert "ev.target.isConnected" in js  # a click on the re-rendered switcher label is not an outside click
    assert "--on-warn:" in css and "color: #fff;" not in css
    assert ".btn.ghost .ic" not in css and "border-radius: 8px" not in css
    assert "changeCount: 0" in js and 'selectTab("inspector", S.inspectorTab)' in js
    assert '"focusout"' in js and '$("#btn-diff-back").focus(' in js
    assert "outline: 0" not in css.split(".filter:focus-within", 1)[1].split("}", 1)[0]  # the wrapper carries the ring
    assert 'class="row-c" role="option" tabindex="0"' not in js  # the commit list is one Tab stop (roving tabindex)


def test_review_tab_structure():
    html = (STATIC / "index.html").read_text(encoding="utf-8"); js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="review"' in html and 'id="pane-review"' in html and 'data-rail="review"' in html and 'id="badge-review"' in html
    pane = html.split('id="pane-review"', 1)[1].split('id="pane-credentials"', 1)[0]
    for i in ("review-source", "review-source-note", "review-mode", "review-draft", "review-gh", "btn-review-run", "review-error", "review-history", "review-empty"):
        assert f'id="{i}"' in pane, i
    for s in ("worktree", "commit", "branch", "pr"): assert f'data-source="{s}"' in pane
    for m in ("code", "received", "mine"): assert f'data-mode="{m}"' in pane
    assert '"review"' in js.split("const INSPECTOR_TABS", 1)[1].split("\n", 1)[0]
    assert "function runReview" in js and "function refreshReviews" in js and "function loadPrs" in js
    assert "<dt>5</dt>" in html or "<dt>1-5</dt>" in html or "<dt>1–5</dt>" in html
    repo_fn = js.split("async function selectRepo", 1)[1].split("async function", 1)[0]
    assert "S.prs = null" in repo_fn and "S.review.pr = null" in repo_fn  # the PR list belongs to one repository


def test_report_view_markup_and_handlers():
    html = (STATIC / "index.html").read_text(encoding="utf-8"); js = (STATIC / "app.js").read_text(encoding="utf-8")
    center = html.split('id="center"', 1)[1].split('class="inspector"', 1)[0]
    for i in ("report-view", "btn-report-back", "report-title", "report-sub", "report-summary", "report-body"): assert f'id="{i}"' in center, i
    assert "function openReport" in js and "function closeReport" in js and 'kind: "review"' in js
    assert "Not sent to the model" in js and "ignored the format" in js
    readme = (STATIC.parent.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "### Review" in readme and "gh" in readme.split("### Review", 1)[1][:3000]


def test_review_tab_keyboard_and_selection_polish():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function rovingTabindex" in js and "function radioKeys" in js
    assert 'rovingTabindex($("#review-source"))' in js and 'rovingTabindex($("#review-mode"))' in js
    assert '$("#review-source").addEventListener("keydown", radioKeys)' in js and '$("#review-mode").addEventListener("keydown", radioKeys)' in js
    for key in ("ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown"): assert key in js.split("function radioKeys", 1)[1].split("\n  }", 1)[0]
    close = js.split("function closeReport", 1)[1].split("\n  }", 1)[0]
    assert "S.reviewSel = null" in close and "renderReviewHistory()" in close
    assert "renderReviewTab()" in js.split("async function showProposal", 1)[1].split("\n", 2)[1]
    r_key = [l for l in js.splitlines() if 'ev.key === "r"' in l][0]
    assert "want.disabled" in r_key
    render = js.split("function renderReviewTab", 1)[1].split("\n  function ", 1)[0]
    assert render.count('#review-mode [data-mode]') == 1  # one loop sets the mode buttons
    readme = (STATIC.parent.parent.parent / "README.md").read_text(encoding="utf-8")
    assert "any file diff" in readme


def test_review_acceptance_fixes():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    render = js.split("function renderReviewTab", 1)[1].split("\n  function ", 1)[0]
    gh_text = js.split("function ghText", 1)[1].split("\n  }", 1)[0]
    assert "ghText(gh)" in render and '"gh is not installed"' in gh_text and "gh auth login" in gh_text  # the PR option's title names the actual reason
    readme = (STATIC.parent.parent.parent / "README.md").read_text(encoding="utf-8")
    gh_par = readme.split("Pull requests are read through", 1)[1].split("\n\n", 1)[0]
    assert "auth status" not in gh_par and "`api user`" in gh_par and "open pull requests" in gh_par


def test_final_review_fixes():
    js = (STATIC / "app.js").read_text(encoding="utf-8"); html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8"); report = (STATIC / "report.js").read_text(encoding="utf-8")
    # 3: the gh login is fetched by the page, not carried by /state; the PR refresh bypasses the cache
    loader = js.split("async function loadGhStatus", 1)[1].split("\n  }", 1)[0]
    assert '"/api/github/status"' in loader and '"?refresh=1"' in loader
    assert "loadGhStatus(true)" in js and "loadGhStatus(false)" in js
    # 5a: a finding opens its file at its line
    assert 'row.classList.add("hit")' in js and 'scrollIntoView({ block: "center" })' in js and "REPORT.newLineIndex" in js
    assert "data-rline" in report and "dataset.rline" in js and ".diff .hit" in css
    # 5b: counts before the summary
    assert "REPORT.summaryLine(review)" in js and "function countsLine" in report
    # 7: a review blocks the assistant buttons and the other way round
    busy = js.split("function setBusy", 1)[1].split("\n  }", 1)[0]
    assert '"apply", "review"]' in busy
    assert "othersBusy" in js.split("function renderReviewTab", 1)[1].split("\n  function ", 1)[0]
    # 8: the store must exist first
    assert 'id="review-store-missing"' in html and 'id="btn-review-init"' in html and "Initialize the store first" in html
    # 9, 10: unparsed reviews warn and still show the stored notes
    assert 'status === "unparsed") toast("warn"' in js
    assert 'html = cards + \'<pre class="diff report-raw">\'' in js
    # 11: the review error is shown inline
    assert '"Reviewing", err)' in js
    # 14: css
    assert ".hist-status.manual" not in css and ".hist-status.unparsed" in css and ".sev.low" in css
    # 15: report.js is loaded before app.js
    assert html.index('src="/report.js"') < html.index('src="/app.js"') and "upstream default" not in js
