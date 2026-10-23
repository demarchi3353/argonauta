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
