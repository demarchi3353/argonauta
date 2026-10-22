"""Claude Code CLI provider: runs the installed `claude` headless under the
user's own login, so proposals are covered by their Claude subscription.

Nothing is extracted from the CLI's credentials; Argonauta only invokes the
documented `claude -p` mode. Tools are disabled, no session is persisted, no
project settings are loaded, and the command runs in an empty temporary
directory, so the call is a plain question-and-answer with the same prompt
the other providers get. The prompt travels on stdin, never in argv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from argonauta.config import Settings
from argonauta.prompts import SYSTEM_PROMPT, ProposalRequest, build_user_message
from argonauta.providers.base import ProgressCallback, ProviderError, ProviderReply, ProviderUnavailable

REQUEST_TIMEOUT = 900
INSTALL_HINT = "Install Claude Code (https://claude.com/claude-code), run `claude auth login`, or set ARGONAUTA_CLAUDE_BIN to the binary."


def find_claude_bin() -> str | None:
    override = os.environ.get("ARGONAUTA_CLAUDE_BIN", "").strip()
    if override:
        return override
    return shutil.which("claude")


def _clean_env() -> dict[str, str]:
    # Drop the markers that make `claude` refuse to start inside another Claude Code session.
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE") and k != "CLAUDE_CODE_ENTRYPOINT"}


def auth_status(timeout: int = 8) -> dict:
    """{installed, logged_in, auth_method} for the settings dialog; never raises."""
    binary = find_claude_bin()
    info: dict = {"installed": bool(binary and os.path.isfile(binary) and os.access(binary, os.X_OK)), "logged_in": None, "auth_method": None}
    if not info["installed"]:
        return info
    try:
        proc = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=timeout, env=_clean_env())
        data = json.loads(proc.stdout)
        info["logged_in"] = bool(data.get("loggedIn"))
        info["auth_method"] = data.get("authMethod")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    return info


class ClaudeCliProvider:
    name = "claude-cli"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.binary = find_claude_bin()
        if not self.binary or not os.path.isfile(self.binary) or not os.access(self.binary, os.X_OK):
            raise ProviderUnavailable(f"The Claude Code CLI (`claude`) was not found. {INSTALL_HINT}")

    def propose(self, req: ProposalRequest, on_progress: ProgressCallback | None = None) -> ProviderReply:
        args = [
            self.binary, "-p",
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--setting-sources", "",
            "--system-prompt", SYSTEM_PROMPT,
            "--model", self.settings.model,
        ]
        if on_progress:
            on_progress(f"asking Claude Code ({self.settings.model})")
        with tempfile.TemporaryDirectory(prefix="argonauta-claude-") as cwd:
            try:
                proc = subprocess.run(args, input=build_user_message(req), capture_output=True, text=True, errors="replace", cwd=cwd, env=_clean_env(), timeout=REQUEST_TIMEOUT)
            except FileNotFoundError as exc:
                raise ProviderUnavailable(f"The Claude Code CLI could not be started. {INSTALL_HINT}") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"Claude Code did not answer within {REQUEST_TIMEOUT}s.") from exc
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())[-500:]
            raise ProviderError(f"Claude Code failed with exit code {proc.returncode}: {detail or 'no output'}")
        data = _parse_json(proc.stdout)
        if data is None:
            raise ProviderError("Claude Code did not return JSON; is this Claude Code 2.x? (`claude --version`)")
        result = data.get("result")
        if data.get("is_error"):
            raise ProviderError(f"Claude Code reported an error: {str(result)[:500] or 'unknown'}")
        if not isinstance(result, str) or not result.strip():
            raise ProviderError("Claude Code returned an empty reply.")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        ints = {k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool)}
        input_tokens = ints.get("input_tokens", 0) + ints.get("cache_creation_input_tokens", 0) + ints.get("cache_read_input_tokens", 0)
        model_usage = data.get("modelUsage")
        model = next(iter(model_usage), self.settings.model) if isinstance(model_usage, dict) and model_usage else self.settings.model
        return ProviderReply(text=result, model=model, input_tokens=input_tokens or None, output_tokens=ints.get("output_tokens"))


def _parse_json(stdout: str) -> dict | None:
    text = stdout.strip()
    for candidate in (text, text.splitlines()[-1] if text else ""):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
