import json

import pytest

from argonauta import workflow
from argonauta.config import load_settings
from argonauta.indexer import index_repo
from argonauta.prompts import build_review_message
from argonauta.workflow import ReviewSpec, WorkflowError, review, truncate_diff
from tests.conftest import ROOT, git
from tests.test_github import fake_gh

SAMPLE_REVIEW_REPLY = ROOT / "examples" / "sample_review_reply.md"


@pytest.fixture
def review_env(sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "scripted")
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(SAMPLE_REVIEW_REPLY))
    settings = load_settings(sample_repo)
    store = workflow.init_repo(settings, None)
    index_repo(store, sample_repo, None)
    yield settings, store, sample_repo
    store.close()


def test_truncate_diff_drops_largest_files_first_and_reports_them():
    small = "diff --git a/s b/s\n+1\n"; big = "diff --git a/b b/b\n" + "+x" * 500 + "\n"; mid = "diff --git a/m b/m\n" + "+y" * 100 + "\n"
    kept, dropped = truncate_diff(small + big + mid, limit=len(small) + len(mid) + 5)
    assert dropped == ["b"] and kept == small + mid
    assert truncate_diff(big, limit=10) == ("", ["b"])
    assert truncate_diff("", limit=10) == ("", [])


def test_truncate_diff_splits_only_on_newlines():
    # a form feed (or \x1c, \u2028) inside a line must not start a phantom chunk
    a = "diff --git a/a b/a\n+page one\x0cdiff --git not a header\n"
    b = "diff --git a/b b/b\n" + "+y" * 50 + "\n"
    assert truncate_diff("pre\n" + a + b, limit=10_000) == ("pre\n" + a + b, [])
    kept, dropped = truncate_diff("pre\n" + a + b, limit=len(a) + 10)
    assert dropped == ["b"] and kept == "pre\n" + a


def test_review_code_on_worktree_stores_findings(review_env):
    settings, store, repo = review_env
    (repo / "calc" / "core.py").write_text((repo / "calc" / "core.py").read_text() + "\n# touched\n")
    out = review(settings, store, ReviewSpec(mode="code", source_kind="worktree"))
    assert out.review.status == "ok" and out.review.source_kind == "worktree" and "calc/core.py" in out.review.source_diff
    assert any(f.get("severity") == "high" for f in out.review.findings)
    assert store.list_events(5)[0].kind == "review"


def test_review_branch_falls_back_to_master_and_fails_without_base(review_env):
    settings, store, repo = review_env
    git(repo, "branch", "-m", "master"); git(repo, "checkout", "-q", "-b", "feature"); (repo / "new.txt").write_text("n\n"); git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "feat")
    out = review(settings, store, ReviewSpec(mode="code", source_kind="branch"))
    assert out.review.source_label == "feature vs master" and "new.txt" in out.review.source_diff
    git(repo, "branch", "-m", "master", "trunk")
    with pytest.raises(WorkflowError, match="main or master"):
        review(settings, store, ReviewSpec(mode="code", source_kind="branch"))


def test_review_branch_without_merge_base_is_a_workflow_error(review_env, monkeypatch):
    settings, store, repo = review_env
    def boom(root, base):
        raise workflow.gitops.GitError("fatal: no merge base")
    monkeypatch.setattr(workflow.gitops, "range_diff", boom)
    with pytest.raises(WorkflowError, match="Could not compare .* no merge base"):
        review(settings, store, ReviewSpec(mode="code", source_kind="branch"))


def test_review_received_keeps_comments_on_omitted_files(review_env, monkeypatch, tmp_path):
    settings, store, repo = review_env
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    huge = "diff --git a/big.bin b/big.bin\n" + "+z" * 200_000 + "\n"; small = "diff --git a/a.py b/a.py\n+1\n"
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"pr view": '{"number": 3, "title": "T", "body": "B", "author": {"login": "me"}, "baseRefName": "main", "headRefName": "b", "url": ""}', "pr diff": huge + small,
        "api repos/o/r/pulls/3/comments": '[{"user": {"login": "alice"}, "body": "on big", "path": "big.bin", "line": 1, "original_line": 1, "diff_hunk": "", "created_at": "2026-10-23T10:00:00Z"}]', "api repos/o/r/pulls/3/reviews": "[]", "api repos/o/r/issues/3/comments": "[]"})))
    monkeypatch.setattr(workflow, "DIFF_CHAR_LIMIT", 1000)
    out = review(settings, store, ReviewSpec(mode="received", source_kind="pr", source_ref="3"))
    assert out.review.omitted_files == ["big.bin"] and out.review.comments[0]["file_omitted"] is True and "a.py" in out.review.source_diff
    assert out.request.comments[0].file_omitted is True and out.review.source_label == "PR #3 T"


def test_review_spec_validation(review_env):
    settings, store, repo = review_env
    for spec, msg in [(ReviewSpec("received", "worktree"), "pull request"), (ReviewSpec("mine", "worktree", draft="  "), "draft"), (ReviewSpec("mine", "worktree", draft="x" * 20_001), "20000"), (ReviewSpec("code", "commit", source_ref="zz"), "commit"), (ReviewSpec("code", "pr", source_ref="abc"), "number"), (ReviewSpec("nope", "worktree"), "mode")]:
        with pytest.raises(WorkflowError, match=msg):
            review(settings, store, spec)


def test_review_unparsed_reply_is_kept_raw(review_env, monkeypatch, tmp_path):
    settings, store, repo = review_env
    prose = tmp_path / "prose.md"; prose.write_text("I refuse to answer in JSON.")
    (repo / "touched.txt").write_text("t\n")  # an empty source is refused before the provider runs
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(prose))
    out = review(load_settings(repo), store, ReviewSpec(mode="code", source_kind="worktree"))  # argonauta.config.load_settings re-reads the env
    assert out.review.status == "unparsed" and out.review.raw.startswith("I refuse") and out.review.findings == []


def test_review_caps_comments_and_notes_when_every_file_is_omitted(review_env, monkeypatch, tmp_path):
    settings, store, repo = review_env
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    comments = [{"user": {"login": "u"}, "body": f"c{i}", "path": "big.bin", "line": 1, "diff_hunk": "", "created_at": f"2026-10-23T10:{i // 60:02d}:{i % 60:02d}Z"} for i in range(203)]
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"pr view": '{"number": 4, "title": "Big", "body": "", "author": {"login": "me"}, "baseRefName": "main", "headRefName": "b", "url": ""}',
        "pr diff": "diff --git a/big.bin b/big.bin\n" + "+z" * 2000 + "\n", "api repos/o/r/pulls/4/comments": json.dumps(comments), "api repos/o/r/pulls/4/reviews": "[]", "api repos/o/r/issues/4/comments": "[]"})))
    monkeypatch.setattr(workflow, "DIFF_CHAR_LIMIT", 1000)
    out = review(settings, store, ReviewSpec(mode="received", source_kind="pr", source_ref="4"))
    assert len(out.request.comments) == workflow.COMMENT_LIMIT and out.request.comments[-1].body == "c199" and all(c.file_omitted for c in out.request.comments)
    assert out.request.notes == ["3 later comments were not sent", "every file was omitted because the diff exceeds the limit"]
    assert out.review.source_diff == "" and out.review.findings[-1] == {"kind": "finding", "severity": "low", "title": "Not everything was sent", "body": "\n".join(out.request.notes)}


def test_review_records_a_failed_row_when_the_provider_errors(review_env, monkeypatch, tmp_path):
    settings, store, repo = review_env
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(tmp_path / "missing.md"))
    (repo / "touched.txt").write_text("t\n")
    with pytest.raises(WorkflowError, match="ARGONAUTA_SCRIPTED_DIFF"):
        review(load_settings(repo), store, ReviewSpec(mode="code", source_kind="worktree"))
    row = store.list_reviews()[0]
    assert row.status == "failed" and "ARGONAUTA_SCRIPTED_DIFF" in row.raw and row.findings == []


def test_review_event_counts_findings_and_disagreements(review_env):
    settings, store, repo = review_env
    (repo / "untracked.txt").write_text("u\n")
    out = review(settings, store, ReviewSpec(mode="code", source_kind="worktree"))
    assert "untracked.txt" in out.review.source_diff
    assert store.list_events(1)[0].message == f"review {out.review.id}: code of Working tree, 2 findings, 1 disagree"


def _review_chunk(source_diff: str, path: str) -> str | None:
    """The same lookup as GET /reviews/{id}/diff?path=."""
    import re
    for chunk in re.split(r"(?m)^(?=diff --git )", source_diff or ""):
        if chunk.split("\n", 1)[0].endswith(f" b/{path}"):
            return chunk
    return None


def test_review_commit_source(review_env):
    settings, store, repo = review_env
    (repo / "calc" / "core.py").write_text((repo / "calc" / "core.py").read_text() + "\n# commit me\n")
    git(repo, "add", "calc/core.py"); git(repo, "commit", "-q", "-m", "Touch core")
    sha = git(repo, "rev-parse", "HEAD").strip()
    out = review(settings, store, ReviewSpec(mode="code", source_kind="commit", source_ref=sha))
    assert out.review.source_label == f"commit {sha[:7]} Touch core" and out.review.source_ref == sha
    assert _review_chunk(out.review.source_diff, "calc/core.py") and "+# commit me" in out.review.source_diff


def test_review_merge_commit_diffs_against_first_parent(review_env):
    settings, store, repo = review_env
    base = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    core = repo / "calc" / "core.py"; original = core.read_text(); first = original.split("\n", 1)
    git(repo, "checkout", "-q", "-b", "side")
    core.write_text("# side change\n" + first[1]); (repo / "side.txt").write_text("from side\n")
    git(repo, "add", "calc/core.py", "side.txt"); git(repo, "commit", "-q", "-m", "side")
    git(repo, "checkout", "-q", base)
    core.write_text("# main change\n" + first[1])
    git(repo, "add", "calc/core.py"); git(repo, "commit", "-q", "-m", "main")
    proc = __import__("subprocess").run(["git", "merge", "-q", "side"], cwd=repo, capture_output=True, text=True)
    assert proc.returncode != 0  # the same line changed on both sides: a conflict
    core.write_text("# resolved change\n" + first[1])
    git(repo, "add", "calc/core.py", "side.txt"); git(repo, "commit", "-q", "--no-edit")
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert len(git(repo, "rev-list", "--parents", "-n", "1", sha).split()) == 3
    out = review(settings, store, ReviewSpec(mode="code", source_kind="commit", source_ref=sha))
    assert "diff --cc" not in out.review.source_diff
    chunk = _review_chunk(out.review.source_diff, "calc/core.py")
    assert chunk and "-# main change" in chunk and "+# resolved change" in chunk
    assert _review_chunk(out.review.source_diff, "side.txt")


def test_truncate_diff_cuts_a_headerless_diff_to_the_limit():
    text = "".join(f"+line {i} " + "x" * 90 + "\n" for i in range(8000))  # ~800 KB, no `diff --git` header
    assert len(text) > 800_000
    notes: list[str] = []
    kept, dropped = truncate_diff(text, notes=notes)
    assert len(kept) <= workflow.DIFF_CHAR_LIMIT and kept.endswith("\n") and text.startswith(kept)
    assert dropped == [] and notes == [workflow.HEADERLESS_CUT_NOTE]
    # a header-less preamble in front of files is cut too, after every file was dropped
    notes = []
    kept, dropped = truncate_diff(text + "diff --git a/a b/a\n+1\n", limit=1000, notes=notes)
    assert len(kept) <= 1000 and dropped == ["a"] and notes == [workflow.HEADERLESS_CUT_NOTE]
    # within the limit nothing is noted
    notes = []
    assert truncate_diff("pre\n", limit=10, notes=notes) == ("pre\n", []) and notes == []


def test_review_never_sends_env_or_protected_files(review_env):
    settings, store, repo = review_env
    (repo / ".env").write_text("SECRET_TOKEN=abc123\n")
    (repo / "calc" / ".env.local").write_text("OTHER_SECRET=1\n")
    (repo / "calc" / "core.py").write_text((repo / "calc" / "core.py").read_text() + "\n# touched\n")
    out = review(settings, store, ReviewSpec(mode="code", source_kind="worktree"))
    assert "SECRET" not in out.review.source_diff and "SECRET" not in build_review_message(out.request)
    assert ".env" in out.review.omitted_files and "calc/.env.local" in out.review.omitted_files
    assert "calc/core.py" in out.review.source_diff and ".argonauta/" not in out.review.source_diff
    assert workflow._excluded_review_path(".git/config") and workflow._excluded_review_path(".argonauta/db.sqlite")
    assert workflow._excluded_review_path("a/b/.envrc") and not workflow._excluded_review_path("src/env.py")


def test_review_of_an_empty_source_is_refused(review_env, monkeypatch, tmp_path):
    settings, store, repo = review_env
    (repo / ".env").write_text("ONLY_SECRET=1\n")  # the only change is excluded, so nothing is left
    with pytest.raises(WorkflowError, match="Nothing to review: the source diff is empty"):
        review(settings, store, ReviewSpec(mode="code", source_kind="worktree"))
    assert store.list_reviews() == []
    # received comments on a PR with an empty diff still go through
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"pr view": '{"number": 6, "title": "T", "body": "", "author": {"login": "me"}, "baseRefName": "main", "headRefName": "b", "url": ""}', "pr diff": "",
        "api repos/o/r/pulls/6/comments": "[]", "api repos/o/r/pulls/6/reviews": "[]", "api repos/o/r/issues/6/comments": '[{"user": {"login": "a"}, "body": "why?", "created_at": "2026-10-23T10:00:00Z"}]'})))
    out = review(settings, store, ReviewSpec(mode="received", source_kind="pr", source_ref="6"))
    assert out.review.status in ("ok", "unparsed") and out.request.comments[0].body == "why?"
