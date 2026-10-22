import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PROJECT = ROOT / "examples" / "sample_project"
SAMPLE_REPLY = ROOT / "examples" / "sample_reply.md"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ARGONAUTA_PROVIDER", "ARGONAUTA_SCRIPTED_DIFF",
                 "ARGONAUTA_TEST_COMMAND", "ARGONAUTA_MODEL", "ARGONAUTA_EFFORT", "ARGONAUTA_FALLBACKS",
                 "ARGONAUTA_MAX_CONTEXT_FILES", "ARGONAUTA_MAX_CONTEXT_BYTES", "ARGONAUTA_TEST_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def sample_repo(tmp_path: Path, clean_env) -> Path:
    """A git repo containing a committed copy of the sample project."""
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE_PROJECT, repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sample project")
    return repo
