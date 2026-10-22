"""User-level model settings (~/.config/argonauta/settings.json): the page can
point the local provider at another machine without touching any repository."""
import json
import stat

import pytest

from argonauta.config import load_settings
from argonauta.user_settings import UserSettingsError, load_user_settings, save_user_settings, settings_path
from tests.test_api import api  # noqa: F401
from tests.test_local_provider import FakeServer


def test_defaults_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path))
    assert load_user_settings() == {"provider": "auto", "local_url": None, "local_model": None, "local_allow_remote": False}
    save_user_settings({"local_url": "http://192.168.0.10:11434/v1", "local_model": "qwen2.5-coder:7b", "local_allow_remote": True})
    assert stat.S_IMODE(settings_path().stat().st_mode) == 0o600
    assert load_user_settings()["local_url"] == "http://192.168.0.10:11434/v1"


def test_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path))
    with pytest.raises(UserSettingsError, match="http"):
        save_user_settings({"local_url": "ftp://x/v1", "local_model": "m", "local_allow_remote": True})
    with pytest.raises(UserSettingsError, match="loopback"):
        save_user_settings({"local_url": "http://192.168.0.10:11434/v1", "local_model": "m", "local_allow_remote": False})
    with pytest.raises(UserSettingsError, match="model"):
        save_user_settings({"local_url": "http://127.0.0.1:11434/v1", "local_model": "has space", "local_allow_remote": False})
    (tmp_path / "settings.json").write_text("{broken")
    with pytest.raises(UserSettingsError, match="not valid"):
        load_user_settings()


def test_precedence_shell_then_user_file_then_default(tmp_path, sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path))
    for name in ("ARGONAUTA_LOCAL_URL", "ARGONAUTA_LOCAL_MODEL", "ARGONAUTA_LOCAL_ALLOW_REMOTE"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings(sample_repo)
    assert s.local_url == "http://127.0.0.1:11434/v1" and s.local_model is None and s.effective_provider == "manual"
    save_user_settings({"local_url": "http://192.168.0.10:11434/v1", "local_model": "qwen2.5-coder:7b", "local_allow_remote": True})
    s = load_settings(sample_repo)
    assert s.local_url == "http://192.168.0.10:11434/v1" and s.local_model == "qwen2.5-coder:7b" and s.effective_provider == "local"
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "from-shell")
    assert load_settings(sample_repo).local_model == "from-shell"  # the shell still wins


def test_model_settings_api(api, monkeypatch):
    for name in ("ARGONAUTA_LOCAL_URL", "ARGONAUTA_LOCAL_MODEL", "ARGONAUTA_LOCAL_ALLOW_REMOTE", "ANTHROPIC_API_KEY", "ARGONAUTA_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    status, data = api.get("/api/settings/model")
    assert status == 200 and data["local_url"] == "http://127.0.0.1:11434/v1" and data["effective_provider"] == "manual"
    assert data["sources"]["local_url"] == "default"
    # a remote host without the explicit flag is refused; nothing is written
    assert api.request("PUT", "/api/settings/model", {"local_url": "http://192.168.0.10:11434/v1", "local_model": "qwen", "local_allow_remote": False})[0] == 400
    assert api.get("/api/settings/model")[1]["local_model"] is None
    status, data = api.request("PUT", "/api/settings/model", {"local_url": "http://192.168.0.10:11434/v1", "local_model": "qwen2.5-coder:7b", "local_allow_remote": True})
    assert status == 200 and data["effective_provider"] == "local" and data["sources"]["local_url"] == "settings"
    assert "192.168.0.10" in api.get(f"/api/repos/{api.repo.id}/state")[1]["store"]["provider"]
    # the shell variable, when set, is reported as the source and wins
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "from-shell")
    data = api.get("/api/settings/model")[1]
    assert data["local_model"] == "from-shell" and data["sources"]["local_model"] == "shell"


def test_model_connection_test_lists_models(api, monkeypatch):
    for name in ("ARGONAUTA_LOCAL_URL", "ARGONAUTA_LOCAL_ALLOW_REMOTE"):
        monkeypatch.delenv(name, raising=False)
    fake = FakeServer(body={"object": "list", "data": [{"id": "qwen2.5-coder:7b"}, {"id": "llama3.1:8b"}]})
    try:
        status, data = api.post("/api/settings/model/test", {"local_url": fake.url, "local_allow_remote": False})
        assert status == 200 and data["ok"] is True and data["models"] == ["qwen2.5-coder:7b", "llama3.1:8b"]
    finally:
        fake.stop()
    status, data = api.post("/api/settings/model/test", {"local_url": "http://127.0.0.1:9/v1", "local_allow_remote": False})
    assert status == 200 and data["ok"] is False and "reach" in data["error"]
    assert api.post("/api/settings/model/test", {"local_url": "http://10.0.0.5:11434/v1", "local_allow_remote": False})[0] == 400


def test_model_settings_api_reports_claude_cli_and_accepts_provider(api, monkeypatch, tmp_path):
    import json as _json
    from tests.test_claude_cli_provider import fake_claude
    for name in ("ARGONAUTA_PROVIDER", "ANTHROPIC_API_KEY", "ARGONAUTA_LOCAL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(fake_claude(tmp_path, _json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))))
    data = api.get("/api/settings/model")[1]
    assert data["claude_cli"]["installed"] is True and data["claude_cli"]["logged_in"] is True and data["claude_cli"]["auth_method"] == "claude.ai"
    assert data["provider_choice"] == "auto"
    status, data = api.request("PUT", "/api/settings/model", {"provider": "claude-cli", "local_url": "", "local_model": "", "local_allow_remote": False})
    assert status == 200 and data["provider_choice"] == "claude-cli" and data["effective_provider"] == "claude-cli"
    assert api.request("PUT", "/api/settings/model", {"provider": "weird", "local_url": "", "local_model": "", "local_allow_remote": False})[0] == 400
