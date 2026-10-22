import os
import stat

from argonauta.web import app as web_app


def test_browser_is_opened_through_an_owner_only_redirect_file(monkeypatch):
    opened = []
    monkeypatch.setattr(web_app.webbrowser, "open", lambda target: opened.append(target) or True)
    url = "http://127.0.0.1:1234/#token=SECRET-TOKEN"
    path = web_app.open_in_browser(url)
    try:
        assert opened and opened[0].startswith("file://") and "SECRET-TOKEN" not in opened[0]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        text = path.read_text()
        assert "SECRET-TOKEN" in text and "http-equiv" in text
    finally:
        web_app.remove_launcher(path)
    assert not path.exists() and not path.parent.exists()
