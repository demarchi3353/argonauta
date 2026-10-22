import os
import subprocess
from pathlib import Path

import pytest

from argonauta import gitops
from tests.conftest import GIT_ENV, git


@pytest.fixture
def repo(sample_repo: Path) -> Path:
    return sample_repo


def test_list_branches_marks_current_and_upstream(repo, tmp_path):
    git(repo, "branch", "feature")
    bare = tmp_path / "remote.git"
    git(repo, "init", "--bare", "-q", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "HEAD")
    branches = gitops.list_branches(repo)
    names = {b.name: b for b in branches}
    current = [b for b in branches if b.is_current]
    assert len(current) == 1 and current[0].upstream == f"origin/{current[0].name}"
    assert names["feature"].remote is None and not names["feature"].is_current
    remote = [b for b in branches if b.remote == "origin"]
    assert remote and remote[0].name.startswith("origin/")
    git(repo, "remote", "set-head", "origin", "-a")
    assert all(b.name != "origin" for b in gitops.list_branches(repo))  # origin/HEAD is not a branch
    assert gitops.list_remotes(repo) == [gitops.RemoteInfo(name="origin", url=str(bare))]


def test_status_detail_splits_areas(repo):
    (repo / "calc" / "core.py").write_text("changed\n")
    (repo / "new.txt").write_text("hi\n")
    (repo / "README.md").write_text("staged\n")
    git(repo, "add", "README.md")
    entries = {e.path: e for e in gitops.status_detail(repo)}
    assert entries["calc/core.py"].worktree == "M" and entries["calc/core.py"].index == " "
    assert entries["README.md"].index == "M" and entries["README.md"].worktree == " "
    assert entries["new.txt"].untracked and not entries["new.txt"].conflicted


def test_graph_log_has_parents_refs_and_signature_code(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("f\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "feature commit")
    commits = gitops.graph_log(repo)
    assert commits[0].subject == "feature commit"
    assert commits[0].parents == [commits[1].sha]
    assert any("feature" in r for r in commits[0].refs)
    assert commits[0].signature in ("N", "G", "B", "U", "X", "Y", "R", "E")
    assert commits[-1].parents == []


def test_commit_detail_lists_files_and_diff(repo):
    (repo / "calc" / "core.py").write_text("changed\n")
    git(repo, "commit", "-q", "-am", "edit core")
    sha = git(repo, "rev-parse", "HEAD").strip()
    detail = gitops.commit_detail(repo, sha)
    assert detail.commit.sha == sha and detail.commit.subject == "edit core"
    assert [f.path for f in detail.files] == ["calc/core.py"]
    assert detail.files[0].deletions > 0 and "diff --git a/calc/core.py" in detail.diff


def test_diff_file_for_untracked_and_index(repo):
    (repo / "new.txt").write_text("hello\n")
    assert "+hello" in gitops.diff_file(repo, "new.txt", "worktree")
    (repo / "README.md").write_text("staged\n")
    git(repo, "add", "README.md")
    assert "+staged" in gitops.diff_file(repo, "README.md", "index")
    assert gitops.diff_file(repo, "README.md", "worktree") == ""


def test_stage_unstage_discard_and_commit(repo):
    (repo / "README.md").write_text("x\n")
    (repo / "junk.txt").write_text("junk\n")
    gitops.stage(repo, ["README.md"])
    assert {e.path: e.index for e in gitops.status_detail(repo)}["README.md"] == "M"
    gitops.unstage(repo, ["README.md"])
    assert {e.path: e.worktree for e in gitops.status_detail(repo)}["README.md"] == "M"
    gitops.discard(repo, ["README.md", "junk.txt"])
    assert gitops.status_detail(repo) == []
    (repo / "README.md").write_text("y\n")
    gitops.stage(repo, ["README.md"])
    sha = gitops.commit_message(repo, "manual commit")
    assert git(repo, "log", "-1", "--format=%s").strip() == "manual commit" and len(sha) == 40


def test_branch_lifecycle_and_name_validation(repo):
    assert gitops.valid_branch_name(repo, "feat/ok")
    assert not gitops.valid_branch_name(repo, "bad name")
    assert not gitops.valid_branch_name(repo, "a..b")
    gitops.create_branch(repo, "feat/ok")
    gitops.checkout(repo, "feat/ok")
    assert gitops.current_branch(repo) == "feat/ok"
    with pytest.raises(gitops.GitError):
        gitops.delete_branch(repo, "feat/ok")  # current branch
    gitops.checkout(repo, "main" if "main" in git(repo, "branch").split() else "master")
    gitops.delete_branch(repo, "feat/ok")
    assert "feat/ok" not in [b.name for b in gitops.list_branches(repo)]


def test_fetch_pull_push_with_local_bare_remote(repo, tmp_path):
    bare = tmp_path / "remote.git"
    git(repo, "init", "--bare", "-q", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    out = gitops.push(repo, set_upstream=True)
    assert "origin" in out or out == ""
    assert gitops.fetch(repo) is not None
    assert gitops.pull(repo) is not None
    assert gitops.ahead_behind(repo) == (0, 0)


def test_remote_ops_do_not_prompt(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "ssh://git@127.0.0.1:9/none.git")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=2")
    with pytest.raises(gitops.GitError):
        gitops.fetch(repo)


@pytest.mark.parametrize("call", [
    lambda r: gitops.checkout(r, "--help"),
    lambda r: gitops.commit_detail(r, "--output=/tmp/argonauta-smuggled"),
    lambda r: gitops.signature_status(r, "--output=/tmp/argonauta-smuggled"),
    lambda r: gitops.delete_branch(r, "--all"),
    lambda r: gitops.create_branch(r, "ok", "--help"),
    lambda r: gitops.fetch(r, "--help"),
])
def test_refs_starting_with_dash_are_not_treated_as_flags(repo, call):
    with pytest.raises(gitops.GitError):
        call(repo)
    assert not Path("/tmp/argonauta-smuggled").exists()


def test_checkout_never_treats_a_file_path_as_a_branch(repo):
    (repo / "README.md").write_text("local edit\n")
    with pytest.raises(gitops.GitError):
        gitops.checkout(repo, "README.md")
    assert (repo / "README.md").read_text() == "local edit\n"  # untouched


def test_discard_drops_staged_changes_and_added_files(repo):
    (repo / "README.md").write_text("staged edit\n")
    (repo / "added.txt").write_text("brand new\n")
    git(repo, "add", "README.md", "added.txt")
    gitops.discard(repo, ["README.md", "added.txt"])
    assert gitops.status_detail(repo) == []
    assert "staged edit" not in (repo / "README.md").read_text()
    assert not (repo / "added.txt").exists()


def test_run_git_in_a_file_path_is_a_git_error(repo):
    with pytest.raises(gitops.GitError, match="cannot run git"):
        gitops.repo_toplevel(repo / "README.md")


def test_apply_accepts_diffs_with_wrong_hunk_counts(repo):
    # Small local models often miscount hunk headers; the body is what matters.
    (repo / "note.txt").write_text("one\ntwo\nthree\n")
    git(repo, "add", "note.txt")
    git(repo, "commit", "-q", "-m", "note")
    miscounted = "diff --git a/note.txt b/note.txt\n--- a/note.txt\n+++ b/note.txt\n@@ -1,9 +1,2 @@\n one\n-two\n+TWO\n three\n"
    assert gitops.apply_check(repo, miscounted) is None
    gitops.apply_diff(repo, miscounted)
    assert (repo / "note.txt").read_text() == "one\nTWO\nthree\n"
    gitops.apply_diff(repo, miscounted, reverse=True)
    assert (repo / "note.txt").read_text() == "one\ntwo\nthree\n"


def test_worktree_lifecycle_inside_dot_worktrees(repo):
    assert [w.path for w in gitops.list_worktrees(repo)] == [repo.resolve()]
    assert gitops.list_worktrees(repo)[0].is_main
    wt = gitops.add_worktree(repo, "feature-x")  # new branch named after the worktree, from HEAD
    assert wt == (repo / ".worktrees" / "feature-x").resolve() and (wt / "calc" / "core.py").is_file()
    assert git(wt, "branch", "--show-current").strip() == "feature-x"
    infos = {w.path: w for w in gitops.list_worktrees(repo)}
    assert infos[wt].branch == "feature-x" and not infos[wt].is_main and infos[wt].head
    assert git(repo, "status", "--porcelain").strip() == ""  # .worktrees/ is excluded, not untracked
    with pytest.raises(gitops.GitError):
        gitops.add_worktree(repo, "feature-x")  # already exists
    for bad in ("", "../x", "a b", ".hidden", "-flag", "x/y"):
        with pytest.raises(gitops.GitError):
            gitops.add_worktree(repo, bad)
    (wt / "note.txt").write_text("dirty\n")
    with pytest.raises(gitops.GitError, match="uncommitted"):
        gitops.remove_worktree(repo, wt)
    gitops.remove_worktree(repo, wt, force=True)
    assert not wt.exists() and [w.path for w in gitops.list_worktrees(repo)] == [repo.resolve()]
    assert "feature-x" in git(repo, "branch").split()  # the branch survives the worktree


def test_worktree_from_existing_branch_and_base(repo):
    git(repo, "branch", "existing")
    wt = gitops.add_worktree(repo, "wt-existing", branch="existing", new_branch=False)
    assert git(wt, "branch", "--show-current").strip() == "existing"
    base = git(repo, "rev-parse", "HEAD").strip()
    wt2 = gitops.add_worktree(repo, "wt-new", branch="topic", base=base)
    assert git(wt2, "branch", "--show-current").strip() == "topic"
    gitops.remove_worktree(repo, wt)
    gitops.remove_worktree(repo, wt2)
