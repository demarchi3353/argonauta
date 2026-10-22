"""User-level settings for the local model provider.

Stored at ~/.config/argonauta/settings.json (0600), outside any repository,
so a cloned repository cannot change them. Shell variables still win.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from argonauta.workspaces import config_dir

FILE_NAME = "settings.json"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
KEYS = ("provider", "local_url", "local_model", "local_allow_remote")
PROVIDER_CHOICES = ("auto", "anthropic", "local", "claude-cli", "manual")


class UserSettingsError(ValueError):
    pass


def settings_path() -> Path:
    return config_dir() / FILE_NAME


def load_user_settings() -> dict:
    path = settings_path()
    data: dict = {"provider": "auto", "local_url": None, "local_model": None, "local_allow_remote": False}
    if not path.is_file():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserSettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise UserSettingsError(f"{path} is not valid: expected an object")
    if isinstance(raw.get("local_url"), str) and raw["local_url"].strip():
        data["local_url"] = raw["local_url"].strip()
    if isinstance(raw.get("local_model"), str) and raw["local_model"].strip():
        data["local_model"] = raw["local_model"].strip()
    data["local_allow_remote"] = raw.get("local_allow_remote") is True
    if raw.get("provider") in PROVIDER_CHOICES:
        data["provider"] = raw["provider"]
    return data


def validate_local_url(url: str, allow_remote: bool) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UserSettingsError("the server URL must be an http(s) URL such as http://192.168.0.10:11434/v1")
    if not allow_remote and parts.hostname not in LOOPBACK_HOSTS:
        raise UserSettingsError(
            f"{parts.hostname!r} is not a loopback address; your code would be sent to that machine. "
            "Tick 'allow a server on another machine' if that is what you want."
        )
    return url.strip().rstrip("/")


def validate_model(model: str | None) -> str | None:
    model = (model or "").strip()
    if not model:
        return None
    if len(model) > 200 or any(ch.isspace() for ch in model):
        raise UserSettingsError("the model name must be a single token without whitespace")
    return model


def save_user_settings(values: dict) -> dict:
    allow_remote = values.get("local_allow_remote") is True
    url = values.get("local_url")
    if url is not None and not isinstance(url, str):
        raise UserSettingsError("local_url must be a string")
    model = values.get("local_model")
    if model is not None and not isinstance(model, str):
        raise UserSettingsError("local_model must be a string")
    provider = values.get("provider", "auto") or "auto"
    if provider not in PROVIDER_CHOICES:
        raise UserSettingsError(f"provider must be one of {', '.join(PROVIDER_CHOICES)}")
    clean = {
        "provider": provider,
        "local_url": validate_local_url(url, allow_remote) if url and url.strip() else None,
        "local_model": validate_model(model),
        "local_allow_remote": allow_remote,
    }
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return clean
