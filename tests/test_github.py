import os
import stat
import subprocess
from pathlib import Path

import pytest

from argonauta import github
from argonauta.github import GitHubError, find_gh_bin, gh_env, gh_status, list_prs, origin_slug, pr_comments, pr_detail, pr_diff
from tests.conftest import git


def fake_gh(tmp_path: Path, responses: dict[str, str]) -> Path:
    """A stand-in `gh` that records argv and env, then prints the response keyed by its first two words."""
    cases = []
    for i, (key, body) in enumerate(responses.items()):
        (tmp_path / f"resp{i}.txt").write_text(body)
        cases.append(f"  '{key}') cat '{tmp_path}/resp{i}.txt' ;;\n")
    script = tmp_path / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{tmp_path}/argv.txt'\n"
        f"env > '{tmp_path}/env.txt'\n"
        "case \"$1 $2\" in\n"
        + "".join(cases)
        + "  *) echo \"no fake response for $1 $2\" >&2; exit 1 ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_gh_env_passes_only_the_allowed_variables(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret"); monkeypatch.setenv("GITHUB_TOKEN", "secret2"); monkeypatch.setenv("GH_HOST", "github.com")
    env = gh_env()
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env and env["GH_HOST"] == "github.com" and "PATH" in env and "HOME" in env
    assert set(env) <= {"HOME", "PATH", "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "GH_HOST"}


@pytest.mark.parametrize("url,slug", [("git@github-personal:demarchi3353/argonauta.git", "demarchi3353/argonauta"), ("git@github.com:o/r", "o/r"), ("ssh://git@github.com/o/r.git", "o/r"), ("https://github.com/o/r.git", "o/r"), ("https://github.com/o/r", "o/r")])
def test_origin_slug_forms(sample_repo, url, slug):
    git(sample_repo, "remote", "add", "origin", url)
    assert origin_slug(sample_repo) == slug


def test_origin_slug_rejects_missing_or_foreign_origin(sample_repo):
    with pytest.raises(GitHubError): origin_slug(sample_repo)
    git(sample_repo, "remote", "add", "origin", "https://gitlab.com/o/r.git")
    with pytest.raises(GitHubError): origin_slug(sample_repo)


def test_list_view_diff_and_comments_use_fixed_arguments(sample_repo, monkeypatch, tmp_path):
    git(sample_repo, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {
        "pr list": '[{"number": 3, "title": "T", "author": {"login": "me"}, "headRefName": "b", "updatedAt": "2026-10-23T00:00:00Z"}]',
        "pr view": '{"number": 3, "title": "T", "body": "B", "author": {"login": "me"}, "baseRefName": "main", "headRefName": "b", "url": "https://github.com/o/r/pull/3"}',
        "pr diff": "diff --git a/a b/a\n",
        "api repos/o/r/pulls/3/comments": '[{"user": {"login": "alice"}, "body": "hm", "path": "a", "line": null, "original_line": 4, "diff_hunk": "@@", "created_at": "2026-10-23T10:00:00Z"}]',
        "api repos/o/r/pulls/3/reviews": '[{"user": {"login": "alice"}, "body": "", "state": "COMMENTED", "submitted_at": "2026-10-23T10:01:00Z"}, {"user": {"login": "bob"}, "body": "LGTM", "state": "APPROVED", "submitted_at": "2026-10-23T09:00:00Z"}]',
        "api repos/o/r/issues/3/comments": '[{"user": {"login": "carol"}, "body": "ping", "created_at": "2026-10-23T11:00:00Z"}]',
    })))
    monkeypatch.setenv("GH_TOKEN", "secret")
    assert list_prs(sample_repo)[0] == {"number": 3, "title": "T", "author": "me", "headRefName": "b", "updatedAt": "2026-10-23T00:00:00Z"}
    assert pr_detail(sample_repo, 3)["author"] == "me" and pr_diff(sample_repo, 3).startswith("diff --git")
    comments = pr_comments(sample_repo, 3)
    assert [c["author"] for c in comments] == ["bob", "alice", "carol"] and comments[1]["line"] == 4 and comments[1]["path"] == "a"
    argv = (tmp_path / "argv.txt").read_text()
    assert "pr list --repo o/r --state open --limit 50 --json" in argv and "pr diff 3 --repo o/r" in argv
    assert "GH_TOKEN" not in (tmp_path / "env.txt").read_text()


def test_pr_number_must_be_a_positive_integer(sample_repo, monkeypatch, tmp_path):
    git(sample_repo, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"pr diff": "x"})))
    for bad in (0, -1, "3; rm", True):
        with pytest.raises(GitHubError, match="pull request number"): pr_diff(sample_repo, bad)


def test_gh_status_reports_login(monkeypatch, tmp_path):
    # only `api user` answers: a broken second account makes `auth status` fail, which must not matter
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"api user": "me\n"})))
    assert gh_status() == {"installed": True, "logged_in": True, "user": "me", "error": None}
    assert "auth status" not in (tmp_path / "argv.txt").read_text()
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(other, {})))
    info = gh_status()
    assert info["installed"] is True and info["logged_in"] is False and "gh auth login" in info["error"]


def test_gh_failures_are_safe_messages(sample_repo, monkeypatch, tmp_path):
    git(sample_repo, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"pr list": "not json"})))
    with pytest.raises(GitHubError, match="not JSON"): list_prs(sample_repo)
    with pytest.raises(GitHubError, match="failed"): pr_diff(sample_repo, 3)
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(tmp_path / "missing-gh"))
    assert gh_status()["installed"] is False
    with pytest.raises(GitHubError, match="not installed"): list_prs(sample_repo)


def test_unexpected_json_shapes_are_gh_errors(sample_repo, monkeypatch, tmp_path):
    git(sample_repo, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {
        "pr list": '{"number": 3}', "pr view": '[1, 2]', "api repos/o/r/pulls/3/comments": '[1]',
    })))
    with pytest.raises(GitHubError, match="unexpected gh output"): list_prs(sample_repo)
    with pytest.raises(GitHubError, match="unexpected gh output"): pr_detail(sample_repo, 3)
    with pytest.raises(GitHubError, match="unexpected gh output"): pr_comments(sample_repo, 3)


def test_bare_gh_override_resolves_through_path(monkeypatch, tmp_path):
    fake_gh(tmp_path, {"api user": "me"})
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", "gh")
    assert find_gh_bin() == str(tmp_path / "gh")
    assert gh_status()["user"] == "me"


def test_gh_runs_with_closed_stdin(monkeypatch):
    seen = {}
    def fake_run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "me", "")
    monkeypatch.setattr(github.subprocess, "run", fake_run)
    monkeypatch.setattr(github, "find_gh_bin", lambda: "/bin/sh")
    github._run(None, ["api", "user"])
    assert seen["stdin"] is subprocess.DEVNULL
