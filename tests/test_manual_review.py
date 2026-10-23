"""The manual provider cannot review: it writes the bundle and says so."""
import pytest

from argonauta.config import load_settings
from argonauta.prompts import ReviewRequest
from argonauta.providers import get_provider
from argonauta.providers.base import ProviderUnavailable


def test_manual_review_writes_a_bundle_and_is_unavailable(sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "manual")
    settings = load_settings(sample_repo)
    req = ReviewRequest(mode="code", source_kind="worktree", source_label="Working tree", diff="diff --git a/a b/a\n", omitted_files=[], comments=[], draft="", pr_title="", pr_body="", repo_summary="root: demo")
    with pytest.raises(ProviderUnavailable) as info:
        get_provider(settings).review(req)
    files = list(settings.prompts_dir.glob("*-review.md"))
    assert len(files) == 1 and "<mode>" in files[0].read_text(encoding="utf-8")
    assert files[0].name in str(info.value) and "cannot be pasted back" in str(info.value)
