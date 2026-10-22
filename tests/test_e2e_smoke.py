"""End-to-end smoke test: drives the installed CLI as a subprocess through the
whole core loop on a throwaway copy of the sample project."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.conftest import GIT_ENV, SAMPLE_PROJECT, SAMPLE_REPLY, git


def run_cli(repo: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update(GIT_ENV)
    env.update({
        "ARGONAUTA_PROVIDER": "scripted",
        "ARGONAUTA_SCRIPTED_DIFF": str(SAMPLE_REPLY),
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [sys.executable, "-m", "argonauta", "--plain", "--repo", str(repo), *args],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert proc.returncode == expect, f"argonauta {' '.join(args)} exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return proc


def test_core_loop_end_to_end(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE_PROJECT, repo)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "sample project")

    # Empty states before init / index.
    out = run_cli(repo, "status").stdout
    assert "not initialised" in out
    run_cli(repo, "history", expect=2)  # no store yet -> invalid state exit code

    run_cli(repo, "init", "--test-command", f"{sys.executable} -m pytest -q")
    assert (repo / ".argonauta" / "argonauta.db").is_file()
    assert "argonauta" not in git(repo, "status", "--porcelain")
    out = run_cli(repo, "history").stdout
    assert "no proposals" in out

    out = run_cli(repo, "index").stdout
    assert "Index ready" in out

    # Validation state: empty task.
    err = run_cli(repo, "propose", "   ", expect=2).stderr
    assert "empty" in err

    out = run_cli(repo, "propose", "Make power() handle negative exponents", "--dry-run").stdout
    assert "calc/core.py" in out and "scripted" in out

    out = run_cli(repo, "propose", "Make power() handle negative exponents").stdout
    assert "Proposal 1 ready" in out and "+    if exponent < 0" in out

    # Confirmation cannot be answered without a terminal: refuse, do not hang.
    err = run_cli(repo, "apply", "1", expect=2).stderr
    assert "--yes" in err
    assert "if exponent < 0" not in (repo / "calc" / "core.py").read_text()

    out = run_cli(repo, "apply", "1", "--yes").stdout
    assert "Applied proposal 1" in out and "Tests passed" in out and "Committed as" in out
    assert "if exponent < 0" in (repo / "calc" / "core.py").read_text()
    assert git(repo, "status", "--porcelain").strip() == ""
    assert "power" in git(repo, "log", "-1", "--format=%s").lower()

    out = run_cli(repo, "show", "1").stdout
    assert "applied" in out and "passed" in out

    out = run_cli(repo, "history", "--events").stdout
    assert "applied" in out and "commit" in out and "Recent commits" in out

    export_path = tmp_path / "export.json"
    out = run_cli(repo, "export", str(export_path)).stdout
    assert "Exported" in out
    data = json.loads(export_path.read_text())
    assert data["format"] == "argonauta-export" and len(data["proposals"]) == 1
    assert data["changes"][0]["commit_sha"]

    # Import into a fresh repository store.
    other = tmp_path / "other"
    shutil.copytree(SAMPLE_PROJECT, other)
    git(other, "init", "-q")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "init")
    run_cli(other, "init")
    run_cli(other, "import", str(tmp_path / "missing.json"), expect=2)
    out = run_cli(other, "import", str(export_path)).stdout
    assert "Imported 1 proposals" in out
    out = run_cli(other, "history").stdout
    assert "negative exponents" in out

    # Sample data loads and clears.
    out = run_cli(other, "sample", "load").stdout
    assert "Loaded sample data" in out
    assert "[SAMPLE]" in run_cli(other, "history").stdout
    run_cli(other, "sample", "clear")
    assert "[SAMPLE]" not in run_cli(other, "history").stdout

    # Logs never contain the diff body or file contents.
    log_text = (repo / ".argonauta" / "argonauta.log").read_text()
    assert "exponent < 0" not in log_text and "propose id=1" in log_text


def test_outside_git_repo_is_a_clear_error(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = subprocess.run([sys.executable, "-m", "argonauta", "--plain", "--repo", str(plain), "status"],
                          capture_output=True, text=True, env={**os.environ, **GIT_ENV, "NO_COLOR": "1"})
    assert proc.returncode == 2 and "not inside a git repository" in proc.stderr
