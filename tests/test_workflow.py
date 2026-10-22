import sys
from pathlib import Path

import pytest

from argonauta.config import load_settings
from argonauta.db import Store
from argonauta.indexer import index_repo
from argonauta.workflow import (
    ValidationError,
    WorkflowError,
    apply_proposal,
    commit_change,
    init_repo,
    propose,
    reject_proposal,
    revert_proposal,
    run_tests_now,
    validate_task,
)
from tests.conftest import SAMPLE_REPLY, git

TEST_CMD = f"{sys.executable} -m pytest -q"


@pytest.fixture
def ready(sample_repo: Path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "scripted")
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(SAMPLE_REPLY))
    settings = load_settings(sample_repo)
    store = init_repo(settings, TEST_CMD)
    index_repo(store, sample_repo)
    yield settings, store
    store.close()


def test_validate_task_rules():
    assert validate_task("  fix it ") == "fix it"
    with pytest.raises(ValidationError):
        validate_task("")
    with pytest.raises(ValidationError):
        validate_task("x" * 5000)
    with pytest.raises(ValidationError):
        validate_task("bad\x07bell")


def test_init_excludes_data_dir_from_git(ready):
    settings, _ = ready
    assert settings.db_path.is_file()
    status = git(settings.repo_root, "status", "--porcelain")
    assert ".argonauta" not in status


def test_dry_run_selects_context_without_calling_provider(ready, monkeypatch):
    settings, store = ready
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", "/nonexistent")
    outcome = propose(settings, store, "power negative exponent", dry_run=True)
    assert outcome.proposal is None
    assert "calc/core.py" in outcome.context.paths
    assert store.list_proposals() == []


def test_propose_requires_index(sample_repo, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "scripted")
    settings = load_settings(sample_repo)
    store = init_repo(settings, None)
    with pytest.raises(WorkflowError, match="index"):
        propose(settings, store, "anything")
    store.close()


def test_full_loop_propose_apply_test_commit_revert(ready):
    settings, store = ready
    outcome = propose(settings, store, "Make power() handle negative exponents")
    p = outcome.proposal
    assert p.status == "proposed" and p.provider == "scripted"
    assert outcome.validation.paths == ["calc/core.py"]

    result = apply_proposal(settings, store, p.id)
    assert result.test_run.status == "passed"
    assert result.commit_sha and result.change.commit_sha == result.commit_sha
    assert store.get_proposal(p.id).status == "applied"
    assert "power" in git(settings.repo_root, "log", "-1", "--format=%s").lower()
    assert "if exponent < 0" in (settings.repo_root / "calc" / "core.py").read_text()

    with pytest.raises(WorkflowError):
        apply_proposal(settings, store, p.id)  # already applied

    change, sha = revert_proposal(settings, store, p.id)
    assert change.reverted_at and sha
    assert "if exponent < 0" not in (settings.repo_root / "calc" / "core.py").read_text()
    assert store.get_proposal(p.id).status == "reverted"
    kinds = [e.kind for e in store.list_events()]
    for expected in ("propose", "apply", "test", "commit", "revert"):
        assert expected in kinds


def test_failed_tests_block_commit_and_can_be_reverted(ready, tmp_path):
    settings, store = ready
    # A diff that applies but breaks the suite.
    bad = ("diff --git a/calc/core.py b/calc/core.py\n--- a/calc/core.py\n+++ b/calc/core.py\n"
           "@@ -1,8 +1,8 @@\n \"\"\"Tiny calculator used as Argonauta sample data. Safe to delete.\"\"\"\n \n \n"
           " def add(a: float, b: float) -> float:\n-    return a + b\n+    return a - b\n \n \n"
           " def subtract(a: float, b: float) -> float:\n")
    diff_file = tmp_path / "bad.diff"
    diff_file.write_text(bad)
    p = propose(settings, store, "break add", from_diff=diff_file).proposal
    assert p.status == "proposed" and p.provider == "manual"
    result = apply_proposal(settings, store, p.id)
    assert result.test_run.status == "failed"
    assert result.commit_sha is None and "nothing was committed" in result.skipped_commit_reason
    assert git(settings.repo_root, "status", "--porcelain").strip() != ""
    change, sha = revert_proposal(settings, store, p.id)
    assert sha is None and change.reverted_at
    assert git(settings.repo_root, "status", "--porcelain").strip() == ""


def test_invalid_diff_is_recorded_not_applied(ready, tmp_path):
    settings, store = ready
    evil = tmp_path / "evil.diff"
    evil.write_text("diff --git a/../outside b/../outside\n--- a/../outside\n+++ b/../outside\n@@ -1 +1 @@\n-a\n+b\n")
    p = propose(settings, store, "escape", from_diff=evil).proposal
    assert p.status == "invalid" and "traversal" in p.notes
    with pytest.raises(WorkflowError):
        apply_proposal(settings, store, p.id)
    assert reject_proposal(store, p.id, "no").status == "rejected"


def test_apply_refuses_dirty_files(ready):
    settings, store = ready
    p = propose(settings, store, "power negative").proposal
    (settings.repo_root / "calc" / "core.py").write_text("# local edit\n" + (settings.repo_root / "calc" / "core.py").read_text())
    with pytest.raises(WorkflowError, match="uncommitted"):
        apply_proposal(settings, store, p.id)


def test_no_commit_then_commit_later(ready):
    settings, store = ready
    p = propose(settings, store, "power negative").proposal
    result = apply_proposal(settings, store, p.id, commit_after=False)
    assert result.commit_sha is None
    sha = commit_change(settings, store, p.id)
    assert store.change_for_proposal(p.id).commit_sha == sha
    with pytest.raises(WorkflowError, match="already committed"):
        commit_change(settings, store, p.id)


def test_run_tests_now_records_failure(ready):
    settings, store = ready
    run = run_tests_now(settings, store)
    assert run.status == "failed" and "test_power_negative" in run.output_tail


def test_sample_proposals_cannot_be_applied(ready):
    from argonauta.sample import clear_samples, load_samples

    settings, store = ready
    load_samples(store)
    sample = [p for p in store.list_proposals() if p.is_sample and p.status == "proposed"][0]
    with pytest.raises(WorkflowError, match="Sample"):
        apply_proposal(settings, store, sample.id)
    assert clear_samples(store)["proposals"] == 3


def test_sloppy_context_diffs_are_accepted_with_a_note(ready, tmp_path):
    settings, store = ready
    # Right change, wrong line numbers and an invented outer context line: what small
    # models produce. Only the context line adjacent to the change is real.
    sloppy = ("diff --git a/calc/core.py b/calc/core.py\n--- a/calc/core.py\n+++ b/calc/core.py\n"
              "@@ -40,5 +40,7 @@\n     return a / b  # not what the file says here\n \n"
              " def power(base: float, exponent: int) -> float:\n"
              "+    if exponent < 0:\n+        return 1.0 / power(base, -exponent)\n     result = 1.0\n")
    path = tmp_path / "sloppy.diff"
    path.write_text(sloppy)
    p = propose(settings, store, "negative exponents", from_diff=path).proposal
    assert p.status == "proposed" and "reduced context" in p.notes
    result = apply_proposal(settings, store, p.id)
    assert result.test_run.status == "passed" and result.commit_sha
    assert "if exponent < 0" in (settings.repo_root / "calc" / "core.py").read_text()


def test_clean_diffs_carry_no_context_note(ready):
    settings, store = ready
    p = propose(settings, store, "power negative").proposal
    assert p.status == "proposed" and p.notes == ""
