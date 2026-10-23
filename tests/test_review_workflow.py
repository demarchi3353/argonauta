import json

import pytest

from argonauta import workflow
from argonauta.config import load_settings
from argonauta.indexer import index_repo
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
