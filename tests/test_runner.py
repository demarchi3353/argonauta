import os
import sys

import pytest

from argonauta.runner import RunnerError, check_command, parse_command


def test_check_command_accepts_resolvable_executables(tmp_path):
    assert check_command(f"{sys.executable} -m pytest -q") == [sys.executable, "-m", "pytest", "-q"]
    assert check_command("git status")[0] == "git"


def test_check_command_rejects_missing_executable_with_a_hint(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(RunnerError, match="command not found: python"):
        check_command("python -m pytest -q")
    with pytest.raises(RunnerError, match="python3"):
        check_command("python -m pytest -q")  # the hint names the usual alternatives


def test_parse_command_still_only_parses():
    assert parse_command("nonexistent-tool --flag") == ["nonexistent-tool", "--flag"]
