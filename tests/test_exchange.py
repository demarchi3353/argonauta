import json

import pytest

from argonauta.db import Store
from argonauta.exchange import ExchangeError, export_data, import_data, load_export
from argonauta.sample import load_samples


def test_export_import_roundtrip(tmp_path):
    src = Store.in_memory()
    src.set_setting("test_command", "pytest -q")
    load_samples(src)
    p = src.create_proposal(task="real", provider="scripted", model="m", status="applied", diff="d")
    c = src.create_change(proposal_id=p.id, files=["a.py"], insertions=1, deletions=0, commit_sha="deadbeef")
    src.create_test_run(proposal_id=p.id, change_id=c.id, started_at="s", finished_at="f", command="pytest", exit_code=0, status="passed", output_tail="ok")
    data = export_data(src)
    path = tmp_path / "export.json"
    path.write_text(json.dumps(data))

    dst = Store.in_memory()
    report = import_data(dst, load_export(path))
    assert report.proposals == 4 and report.changes == 2 and report.test_runs == 3
    assert dst.get_setting("test_command") is None  # executable settings are never imported
    assert report.skipped_settings == {"test_command": "pytest -q"}
    imported = [x for x in dst.list_proposals() if x.task == "real"][0]
    change = dst.change_for_proposal(imported.id)
    assert change.commit_sha == "deadbeef" and change.files == ["a.py"]
    assert dst.latest_test_run_for_proposal(imported.id).status == "passed"

    # Merge again: ids are fresh, nothing is overwritten.
    import_data(dst, load_export(path))
    assert len(dst.list_proposals()) == 8
    # Replace: back to one copy.
    import_data(dst, load_export(path), replace=True)
    assert len(dst.list_proposals()) == 4


@pytest.mark.parametrize("payload,needle", [
    ("[]", "object"),
    ('{"format": "other", "version": 1}', "not an Argonauta export"),
    ('{"format": "argonauta-export", "version": 99}', "unsupported export version"),
    ('{"format": "argonauta-export", "version": 1, "proposals": [{"status": "weird"}]}', "unknown proposal status"),
    ('{"format": "argonauta-export", "version": 1, "proposals": [], "changes": [{"proposal_id": 7}]}', "unknown proposal"),
])
def test_import_rejects_bad_payloads(tmp_path, payload, needle):
    path = tmp_path / "bad.json"
    path.write_text(payload)
    store = Store.in_memory()
    with pytest.raises(ExchangeError, match=needle):
        import_data(store, load_export(path))


def test_import_is_atomic_under_transaction(tmp_path):
    store = Store.in_memory()
    payload = {"format": "argonauta-export", "version": 1,
               "proposals": [{"id": 1, "status": "proposed", "task": "ok"}, {"id": 2, "status": "nonsense"}]}
    path = tmp_path / "x.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ExchangeError):
        with store.transaction():
            import_data(store, load_export(path))
    assert store.list_proposals() == []
