"""Settings loaded from the environment and the repository's .env file.

Nothing here is ever logged. Secrets are only checked for presence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DATA_DIR_NAME = ".argonauta"
DB_FILE_NAME = "argonauta.db"
LOG_FILE_NAME = "argonauta.log"
PROMPTS_DIR_NAME = "prompts"

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
VALID_PROVIDERS = ("auto", "anthropic", "manual", "scripted")
SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# Only these keys are read from a repository's .env. A cloned repository must
# not be able to point the SDK at another endpoint (ANTHROPIC_BASE_URL, proxy
# variables, ...), choose a command for `apply` to execute
# (ARGONAUTA_TEST_COMMAND), or swap the provider for one that replays a file
# (ARGONAUTA_PROVIDER / ARGONAUTA_SCRIPTED_DIFF). Those come only from the
# real shell environment or from `argonauta config`.
DOTENV_ALLOWLIST = (
    "ANTHROPIC_API_KEY",
    "ARGONAUTA_MODEL",
    "ARGONAUTA_EFFORT",
    "ARGONAUTA_FALLBACKS",
    "ARGONAUTA_MAX_CONTEXT_FILES",
    "ARGONAUTA_MAX_CONTEXT_BYTES",
    "ARGONAUTA_TEST_TIMEOUT",
)
# ARGONAUTA_CONFIG_DIR decides which workspace registry the web UI trusts, so a
# cloned repository must not be able to redirect it either.
SHELL_ONLY_VARS = ("ARGONAUTA_TEST_COMMAND", "ARGONAUTA_PROVIDER", "ARGONAUTA_SCRIPTED_DIFF", "ARGONAUTA_CONFIG_DIR")


class ConfigError(ValueError):
    """Raised when an environment value is invalid."""


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    provider: str
    model: str
    effort: str
    fallbacks: str
    has_api_key: bool
    scripted_diff: Path | None
    max_context_files: int
    max_context_bytes: int
    test_command_override: str | None
    test_timeout: int

    @property
    def data_dir(self) -> Path:
        return self.repo_root / DATA_DIR_NAME

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE_NAME

    @property
    def log_path(self) -> Path:
        return self.data_dir / LOG_FILE_NAME

    @property
    def prompts_dir(self) -> Path:
        return self.data_dir / PROMPTS_DIR_NAME

    @property
    def effective_provider(self) -> str:
        if self.provider != "auto":
            return self.provider
        return "anthropic" if self.has_api_key else "manual"


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def load_repo_dotenv(env_file: Path) -> list[str]:
    """Copy allowlisted keys from .env into the environment without overriding the shell.

    Returns the names that were loaded. Unknown keys are ignored on purpose.
    """
    if not env_file.is_file():
        return []
    values = dotenv_values(env_file)
    loaded = []
    for key in DOTENV_ALLOWLIST:
        value = values.get(key)
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_settings(repo_root: Path) -> Settings:
    """Build Settings for a repository, reading its .env without overriding the shell."""
    load_repo_dotenv(repo_root / ".env")

    provider = os.environ.get("ARGONAUTA_PROVIDER", "auto").strip().lower() or "auto"
    if provider not in VALID_PROVIDERS:
        raise ConfigError(
            f"ARGONAUTA_PROVIDER must be one of {', '.join(VALID_PROVIDERS)}, got {provider!r}"
        )

    effort = os.environ.get("ARGONAUTA_EFFORT", DEFAULT_EFFORT).strip().lower() or DEFAULT_EFFORT
    if effort not in VALID_EFFORTS:
        raise ConfigError(
            f"ARGONAUTA_EFFORT must be one of {', '.join(VALID_EFFORTS)}, got {effort!r}"
        )

    fallbacks = os.environ.get("ARGONAUTA_FALLBACKS", "default").strip().lower() or "default"
    if fallbacks not in ("default", "off"):
        raise ConfigError(f"ARGONAUTA_FALLBACKS must be 'default' or 'off', got {fallbacks!r}")

    model = os.environ.get("ARGONAUTA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if len(model) > 100 or any(ch.isspace() for ch in model):
        raise ConfigError("ARGONAUTA_MODEL must be a single model id without whitespace")

    scripted_raw = os.environ.get("ARGONAUTA_SCRIPTED_DIFF", "").strip()
    scripted_diff = Path(scripted_raw).expanduser() if scripted_raw else None

    has_api_key = any(os.environ.get(name, "").strip() for name in SECRET_ENV_VARS)
    override = os.environ.get("ARGONAUTA_TEST_COMMAND", "").strip() or None

    return Settings(
        repo_root=repo_root,
        provider=provider,
        model=model,
        effort=effort,
        fallbacks=fallbacks,
        has_api_key=has_api_key,
        scripted_diff=scripted_diff,
        max_context_files=_int_env("ARGONAUTA_MAX_CONTEXT_FILES", 8, 1, 50),
        max_context_bytes=_int_env("ARGONAUTA_MAX_CONTEXT_BYTES", 60_000, 1_000, 2_000_000),
        test_command_override=override,
        test_timeout=_int_env("ARGONAUTA_TEST_TIMEOUT", 600, 5, 7200),
    )
