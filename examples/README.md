# Sample data

Everything in this directory is **sample data** and safe to delete.

- `sample_project/` is a tiny Python calculator with one deliberate bug:
  `power()` ignores negative exponents, so `tests/test_core.py::test_power_negative`
  fails until the fix is applied.
- `sample_reply.md` is what a model reply looks like: a short explanation followed
  by a fenced unified diff that fixes the bug. The end-to-end test and
  `scripts/demo.sh` feed it to the TEST-ONLY `scripted` provider so the full
  loop runs without any network access.

Inside the app, `argonauta sample load` inserts labelled `[SAMPLE]` records into
the current repository's store, and `argonauta sample clear` removes them.
