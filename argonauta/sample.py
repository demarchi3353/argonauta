"""Clearly labelled sample records. Every row carries is_sample=1 and a
"[SAMPLE]" prefix, and `argonauta sample clear` removes all of them.
"""

from __future__ import annotations

from argonauta.db import Store

SAMPLE_PREFIX = "[SAMPLE] "

SAMPLE_DIFF_RENAME = """diff --git a/calc/core.py b/calc/core.py
--- a/calc/core.py
+++ b/calc/core.py
@@ -10,7 +10,9 @@ def divide(a: float, b: float) -> float:
-    return a / b
+    if b == 0:
+        raise ZeroDivisionError("cannot divide by zero")
+    return a / b
"""

SAMPLE_DIFF_DOCS = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
 # calc
+
+Tiny calculator used as sample data.
"""


def load_samples(store: Store) -> dict[str, int]:
    created = {"proposals": 0, "changes": 0, "test_runs": 0}

    applied = store.create_proposal(
        task=SAMPLE_PREFIX + "Guard divide() against division by zero",
        provider="sample",
        model="sample-model",
        status="applied",
        explanation="Sample record: adds an explicit ZeroDivisionError with a clear message.",
        diff=SAMPLE_DIFF_RENAME,
        context_files=["calc/core.py", "tests/test_core.py"],
        input_tokens=1830,
        output_tokens=212,
        is_sample=True,
        created_at="2026-10-20T09:12:00+00:00",
    )
    created["proposals"] += 1
    change = store.create_change(
        proposal_id=applied.id,
        files=["calc/core.py"],
        insertions=3,
        deletions=1,
        commit_sha="0000000000000000000000000000000000000001",
        is_sample=True,
        applied_at="2026-10-20T09:13:30+00:00",
    )
    created["changes"] += 1
    store.create_test_run(
        proposal_id=applied.id,
        change_id=change.id,
        started_at="2026-10-20T09:13:31+00:00",
        finished_at="2026-10-20T09:13:34+00:00",
        command="python -m pytest -q",
        exit_code=0,
        status="passed",
        output_tail="7 passed in 0.41s",
        is_sample=True,
    )
    created["test_runs"] += 1

    rejected = store.create_proposal(
        task=SAMPLE_PREFIX + "Add a one-line project description to README",
        provider="sample",
        model="sample-model",
        status="rejected",
        explanation="Sample record: the proposal was rejected by the user as unnecessary.",
        diff=SAMPLE_DIFF_DOCS,
        context_files=["README.md"],
        input_tokens=640,
        output_tokens=80,
        notes="not needed right now",
        is_sample=True,
        created_at="2026-10-21T15:40:00+00:00",
    )
    created["proposals"] += 1

    failed = store.create_proposal(
        task=SAMPLE_PREFIX + "Speed up power() with exponentiation by squaring",
        provider="sample",
        model="sample-model",
        status="proposed",
        explanation="Sample record: a pending proposal whose last test run failed.",
        diff="diff --git a/calc/core.py b/calc/core.py\n--- a/calc/core.py\n+++ b/calc/core.py\n@@ -1,1 +1,1 @@\n-# sample\n+# sample (pending)\n",
        context_files=["calc/core.py"],
        input_tokens=1210,
        output_tokens=160,
        is_sample=True,
        created_at="2026-10-22T08:05:00+00:00",
    )
    created["proposals"] += 1
    store.create_test_run(
        proposal_id=failed.id,
        change_id=None,
        started_at="2026-10-22T08:06:00+00:00",
        finished_at="2026-10-22T08:06:03+00:00",
        command="python -m pytest -q",
        exit_code=1,
        status="failed",
        output_tail="FAILED tests/test_core.py::test_power_negative - AssertionError\n1 failed, 6 passed in 0.52s",
        is_sample=True,
    )
    created["test_runs"] += 1

    for pid in (applied.id, rejected.id, failed.id):
        store.record_event("sample.load", f"sample proposal {pid} loaded", "proposals", pid)
    return created


def clear_samples(store: Store) -> dict[str, int]:
    return store.delete_samples()
