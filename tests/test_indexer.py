from pathlib import Path

from argonauta.db import Store
from argonauta.indexer import index_repo, search, should_skip, tokenize
from tests.conftest import git


def test_tokenize_splits_identifiers():
    tokens = tokenize("def computeTotalPrice(order_items): return HTTPServer")
    assert "computetotalprice" in tokens and "compute" in tokens and "total" in tokens and "price" in tokens
    assert "order" in tokens and "items" in tokens and "server" in tokens
    assert "def" not in tokens  # stopword


def test_should_skip_protected_paths():
    assert should_skip(".argonauta/argonauta.db")
    assert should_skip(".env")
    assert should_skip("a/.env.local")
    assert should_skip("node_modules/x/index.js")
    assert not should_skip("src/app.py")


def test_index_and_search_rank_relevant_file(sample_repo: Path):
    store = Store.in_memory()
    stats = index_repo(store, sample_repo)
    assert stats.indexed >= 4 and stats.skipped == 0
    hits = search(store, "power exponent negative", limit=3)
    assert hits and hits[0].file.path == "calc/core.py"
    # Incremental: nothing changes -> nothing re-indexed.
    stats2 = index_repo(store, sample_repo)
    assert stats2.indexed == 0 and stats2.unchanged == stats.indexed
    # Changed file is re-indexed, removed file disappears.
    (sample_repo / "calc" / "core.py").write_text("def unrelated():\n    return 1\n")
    (sample_repo / "README.md").unlink()
    stats3 = index_repo(store, sample_repo)
    assert stats3.indexed == 1 and stats3.removed == 1
    assert not any(h.file.path == "calc/core.py" for h in search(store, "exponent"))
    store.close()


def test_binary_and_large_files_are_skipped(sample_repo: Path, tmp_path):
    (sample_repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    (sample_repo / "big.txt").write_text("x" * 600_000)
    git(sample_repo, "add", "-A")
    store = Store.in_memory()
    stats = index_repo(store, sample_repo)
    assert stats.skipped == 2
    store.close()


def test_vendor_and_license_files_are_not_indexed():
    assert should_skip("argonauta/web/static/vendor/xterm.js")
    assert should_skip("third_party/lib.js")
    assert should_skip("LICENSE")
    assert should_skip("argonauta/web/static/vendor/LICENSE")
    assert should_skip("docs/COPYING.txt")
    assert not should_skip("argonauta/web/static/app.js")
    assert not should_skip("src/vendor_report.py")
