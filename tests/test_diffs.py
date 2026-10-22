from pathlib import Path

from argonauta.diffs import extract_diff, parse_diff, validate_diff

SIMPLE = """diff --git a/calc/core.py b/calc/core.py
--- a/calc/core.py
+++ b/calc/core.py
@@ -1,2 +1,3 @@
 line1
-line2
+line2 changed
+line3
"""


def test_extract_prefers_fenced_diff_block():
    reply = "Here is the fix.\n\n```diff\n" + SIMPLE + "```\n\nHope that helps."
    assert extract_diff(reply) == SIMPLE


def test_extract_falls_back_to_bare_diff():
    assert extract_diff("intro text\n" + SIMPLE) == SIMPLE


def test_extract_returns_empty_when_no_diff():
    assert extract_diff("I cannot do this safely.") == ""


def test_parse_counts_and_modes():
    files = parse_diff(SIMPLE)
    assert len(files) == 1
    f = files[0]
    assert f.path == "calc/core.py" and f.insertions == 2 and f.deletions == 1 and f.hunks == 1
    new_file = "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n"
    files = parse_diff(new_file)
    assert files[0].is_new and files[0].path == "new.txt" and files[0].insertions == 1


def test_parse_plain_unified_diff_without_git_header():
    plain = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-c\n+d\n"
    files = parse_diff(plain)
    assert [f.path for f in files] == ["x.py", "y.py"]


def test_validate_rejects_dangerous_paths(tmp_path: Path):
    for bad in ("../etc/passwd", "/etc/passwd", ".git/config", ".argonauta/argonauta.db", ".env", "sub/.env.local"):
        text = f"diff --git a/{bad} b/{bad}\n--- a/{bad}\n+++ b/{bad}\n@@ -1 +1 @@\n-a\n+b\n"
        result = validate_diff(text, tmp_path)
        assert not result.ok, bad
        assert result.errors


def test_validate_rejects_symlink_escape(tmp_path: Path):
    (tmp_path / "link").symlink_to(tmp_path.parent)
    text = "diff --git a/link/x b/link/x\n--- a/link/x\n+++ b/link/x\n@@ -1 +1 @@\n-a\n+b\n"
    assert not validate_diff(text, tmp_path).ok


def test_validate_requires_existing_file_for_modification(tmp_path: Path):
    assert not validate_diff(SIMPLE, tmp_path).ok
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "core.py").write_text("line1\nline2\n")
    result = validate_diff(SIMPLE, tmp_path)
    assert result.ok and result.paths == ["calc/core.py"] and result.insertions == 2


def test_validate_empty_and_oversized():
    assert validate_diff("   ").errors == ["the diff is empty"]
    huge = SIMPLE + ("+x\n" * 400_000)
    assert "larger" in validate_diff(huge).errors[0]
