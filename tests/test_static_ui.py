import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "argonauta" / "web" / "static"
DYNAMIC_IDS = {"apply-test", "apply-commit"}  # created by app.js at runtime
# XML namespaces and loopback placeholders are not network references
ALLOWED_URL_PREFIXES = ("http://127.0.0.1", "http://localhost", "http://www.w3.org/")


def test_ids_bound_in_app_js_exist_in_index_html():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    html_ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
    # `$("#id")`, `$$("#id …")`; a trailing `-` (as in `"#btn-" + k`) is not an id
    bound = set(re.findall(r"""\$\$?\(\s*['"]#([A-Za-z0-9_-]*[A-Za-z0-9])(?=['"\s\[])""", js))
    missing = sorted(bound - html_ids - DYNAMIC_IDS)
    assert not missing, f"app.js binds ids missing from index.html: {missing}"


def test_index_has_brand_and_no_external_urls():
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "app.css").read_text()
    assert "Argonauta" in html
    urls = re.findall(r"https?://[^\s\"'<>]+", html + css)
    external = [u for u in urls if not u.startswith(ALLOWED_URL_PREFIXES)]
    assert not external, f"external network references in static UI: {external}"


def test_css_declares_slate_tokens_in_both_schemes():
    css = (STATIC / "app.css").read_text()
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
    html = (STATIC / "index.html").read_text()
    assert 'id="repo-switch"' in html and 'aria-haspopup="menu"' in html
    assert 'id="repo-menu"' in html and 'id="workspace-list"' in html
    assert 'id="workspace-select"' not in html and 'id="repo-select"' not in html
    assert 'class="logo" src="/logo.png" alt="" width="34" height="34"' in html
    assert 'id="btn-help"' in html and 'aria-label="Help"' in html
    # the filter lives in the topbar, between the brand and the git actions
    top = html.split('<header', 1)[1].split('</header>', 1)[0]
    assert 'id="commit-filter"' in top and 'id="btn-push"' in top
