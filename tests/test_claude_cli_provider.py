"""The `claude-cli` provider runs the installed Claude Code CLI headless, under
the user's own login. Tested with a fake `claude` executable."""
import json
import os
import stat
from pathlib import Path

import pytest

from argonauta.config import load_settings
from argonauta.prompts import ContextFile, ProposalRequest
from argonauta.providers import describe_provider, get_provider
from argonauta.providers.base import ProviderError, ProviderUnavailable

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fake binaries are sh scripts")

REPLY = "Short explanation.\n\n```diff\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n```\n"


def fake_claude(tmp_path: Path, body: str) -> Path:
    """A stand-in `claude` that records argv, stdin and cwd, then prints `body`."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" > '{tmp_path}/argv.txt'\n"
        f"cat > '{tmp_path}/stdin.txt'\n"
        f"pwd > '{tmp_path}/cwd.txt'\n"
        f"env > '{tmp_path}/env.txt'\n"
        f"cat <<'JSON'\n{body}\nJSON\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _req():
    return ProposalRequest(task="rename x to y", repo_summary="root: demo", files=[ContextFile("a.py", "x\n")])


@pytest.fixture
def cli_env(sample_repo, clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "claude-cli")
    monkeypatch.setenv("CLAUDECODE", "1")  # as when Argonauta itself runs inside Claude Code
    return sample_repo


def test_claude_cli_runs_headless_with_prompt_on_stdin(cli_env, monkeypatch, tmp_path):
    body = json.dumps({"type": "result", "is_error": False, "result": REPLY, "stop_reason": "end_turn",
                       "usage": {"input_tokens": 12, "cache_creation_input_tokens": 600, "cache_read_input_tokens": 0, "output_tokens": 40},
                       "modelUsage": {"claude-opus-5-5": {}}})
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(fake_claude(tmp_path, body)))
    settings = load_settings(cli_env)
    assert settings.effective_provider == "claude-cli"
    assert "Claude Code" in describe_provider(settings) and "subscription" in describe_provider(settings)
    reply = get_provider(settings).propose(_req(), on_progress=lambda m: None)
    assert reply.text == REPLY and reply.model == "claude-opus-5-5"
    assert reply.input_tokens == 612 and reply.output_tokens == 40  # cache-creation tokens are input too
    argv = (tmp_path / "argv.txt").read_text()
    assert "-p" in argv.split() and "--output-format json" in argv and "--tools " in argv and "--no-session-persistence" in argv and "--setting-sources " in argv
    assert "--system-prompt" in argv and "--model claude-opus-5-5" in argv
    stdin = (tmp_path / "stdin.txt").read_text()
    assert "rename x to y" in stdin and "a.py" in stdin and "rename x to y" not in argv  # the prompt never goes through argv
    assert (tmp_path / "cwd.txt").read_text().strip() != str(cli_env)  # runs outside the repository
    env = (tmp_path / "env.txt").read_text()
    assert "CLAUDECODE=" not in env  # nested-session guard variable is stripped


def test_claude_cli_errors_are_messages(cli_env, monkeypatch, tmp_path):
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(tmp_path / "missing-claude"))
    with pytest.raises(ProviderUnavailable, match="Claude Code CLI"):
        get_provider(load_settings(cli_env)).propose(_req())
    body = json.dumps({"type": "result", "is_error": True, "result": "Not logged in. Run claude auth login."})
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(fake_claude(tmp_path, body)))
    with pytest.raises(ProviderError, match="Not logged in"):
        get_provider(load_settings(cli_env)).propose(_req())
    bad = tmp_path / "claude-bad"
    bad.write_text("#!/bin/sh\necho 'boom' >&2\nexit 3\n"); bad.chmod(0o755)
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(bad))
    with pytest.raises(ProviderError, match="exit code 3"):
        get_provider(load_settings(cli_env)).propose(_req())
    notjson = tmp_path / "claude-text"
    notjson.write_text("#!/bin/sh\necho 'plain text'\n"); notjson.chmod(0o755)
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(notjson))
    with pytest.raises(ProviderError, match="JSON"):
        get_provider(load_settings(cli_env)).propose(_req())


def test_user_settings_can_select_the_provider(sample_repo, clean_env, monkeypatch):
    from argonauta.user_settings import save_user_settings

    monkeypatch.delenv("ARGONAUTA_PROVIDER", raising=False)
    assert load_settings(sample_repo).effective_provider == "manual"
    save_user_settings({"provider": "claude-cli", "local_url": None, "local_model": None, "local_allow_remote": False})
    assert load_settings(sample_repo).effective_provider == "claude-cli"
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "manual")
    assert load_settings(sample_repo).effective_provider == "manual"  # the shell still wins
    with pytest.raises(Exception, match="provider"):
        save_user_settings({"provider": "nonsense"})


def _review_req():
    from argonauta.prompts import ReviewRequest
    return ReviewRequest(mode="code", source_kind="worktree", source_label="Working tree", diff="diff --git a/a b/a\n", omitted_files=[], comments=[], draft="", pr_title="", pr_body="", repo_summary="root: demo")


def test_claude_cli_review_uses_the_review_system_prompt(cli_env, monkeypatch, tmp_path):
    from argonauta.prompts import REVIEW_SYSTEM_PROMPT
    body = json.dumps({"type": "result", "is_error": False, "result": "```json\n{\"summary\": \"ok\", \"findings\": []}\n```", "usage": {"input_tokens": 3, "output_tokens": 2}})
    monkeypatch.setenv("ARGONAUTA_CLAUDE_BIN", str(fake_claude(tmp_path, body)))
    reply = get_provider(load_settings(cli_env)).review(_review_req())
    assert "findings" in reply.text
    argv = (tmp_path / "argv.txt").read_text()
    assert REVIEW_SYSTEM_PROMPT.splitlines()[0] in argv and "<mode>" in (tmp_path / "stdin.txt").read_text()
