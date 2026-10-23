import pytest

from argonauta.db import Store, StoreError


@pytest.fixture
def store():
    s = Store.in_memory()
    yield s
    s.close()


def test_schema_version_recorded(store):
    assert store.get_meta("schema_version") == "1"
    assert store.get_meta("created_at")


def test_settings_roundtrip(store):
    assert store.get_setting("test_command") is None
    store.set_setting("test_command", "pytest -q")
    assert store.get_setting("test_command") == "pytest -q"
    store.set_setting("test_command", None)
    assert store.get_setting("test_command") is None


def test_proposal_lifecycle(store):
    p = store.create_proposal(task="do it", provider="scripted", model="m", status="proposed", diff="x", context_files=["a.py"])
    assert p.id == 1 and p.status == "proposed" and p.context_files == ["a.py"]
    assert store.get_proposal(1).task == "do it"
    updated = store.update_proposal_status(1, "rejected", "nope")
    assert updated.status == "rejected" and updated.notes == "nope"
    assert store.count_proposals_by_status() == {"rejected": 1}
    with pytest.raises(StoreError):
        store.update_proposal_status(1, "bogus")
    with pytest.raises(StoreError):
        store.get_proposal(99)


def test_change_and_test_run_links(store):
    p = store.create_proposal(task="t", provider="p", model="m", status="proposed")
    c = store.create_change(proposal_id=p.id, files=["a.py"], insertions=2, deletions=1)
    assert store.change_for_proposal(p.id).id == c.id
    assert c.commit_sha is None
    c = store.set_change_commit(c.id, "abc123")
    assert c.commit_sha == "abc123"
    r = store.create_test_run(proposal_id=p.id, change_id=c.id, started_at="s", finished_at="f", command="pytest", exit_code=0, status="passed", output_tail="ok")
    assert store.latest_test_run_for_proposal(p.id).id == r.id
    c = store.mark_change_reverted(c.id)
    assert c.reverted_at is not None


def test_cascade_delete_of_samples(store):
    p = store.create_proposal(task="[SAMPLE] x", provider="s", model="m", status="applied", is_sample=True)
    c = store.create_change(proposal_id=p.id, files=[], insertions=0, deletions=0, is_sample=True)
    store.create_test_run(proposal_id=p.id, change_id=c.id, started_at="s", finished_at="f", command="c", exit_code=0, status="passed", output_tail="", is_sample=True)
    real = store.create_proposal(task="real", provider="s", model="m", status="proposed")
    counts = store.delete_samples()
    assert counts["proposals"] == 1 and counts["changes"] == 1 and counts["test_runs"] == 1
    assert [p.id for p in store.list_proposals()] == [real.id]


def test_events_are_recorded_in_order(store):
    store.record_event("a", "first")
    store.record_event("b", "second", "proposals", 1)
    events = store.list_events()
    assert [e.kind for e in events] == ["b", "a"]
    assert events[0].ref_table == "proposals" and events[0].ref_id == 1


def test_file_index_upsert_and_postings(store):
    fid = store.upsert_file(path="a.py", size=10, sha256="h1", language="python", line_count=1, terms={"foo": 2, "bar": 1})
    store.upsert_file(path="a.py", size=12, sha256="h2", language="python", line_count=2, terms={"baz": 1})
    assert store.file_count() == 1
    assert store.file_hashes()["a.py"] == (fid, "h2")
    assert "foo" not in store.term_postings(["foo", "baz"])
    assert store.term_postings(["baz"])["baz"] == [(fid, 1)]
    assert store.delete_files(["a.py"]) == 1
    assert store.term_postings(["baz"]) == {}


def test_open_requires_init(tmp_path):
    with pytest.raises(StoreError):
        Store.open(tmp_path / "missing.db")
    s = Store.open(tmp_path / "new.db", create=True)
    s.close()
    assert (tmp_path / "new.db").is_file()


def test_reviews_round_trip(tmp_path):
    store = Store.open(tmp_path / "s.db", create=True)
    r = store.create_review(mode="received", source_kind="pr", source_ref="3", source_label="PR #3", source_diff="diff --git a/x b/x\n", omitted_files=["big.bin"],
                            comments=[{"author": "alice", "body": "hm", "path": "x", "line": 1}], draft="", provider="scripted", model="scripted", status="ok",
                            summary="1 disagree", findings=[{"kind": "verdict", "verdict": "disagree", "title": "t", "body": "b"}], raw="{}", input_tokens=10, output_tokens=5)
    assert r.id == 1 and r.omitted_files == ["big.bin"] and r.comments[0]["author"] == "alice" and r.findings[0]["verdict"] == "disagree"
    assert store.get_review(1).summary == "1 disagree"
    store.create_review(mode="code", source_kind="worktree", source_ref="", source_label="Working tree", source_diff="", omitted_files=[], comments=[], draft="", provider="manual", model="manual", status="unparsed", summary="", findings=[], raw="prose")
    assert [x.id for x in store.list_reviews()] == [2, 1]
    with pytest.raises(StoreError):
        store.get_review(9)
    with pytest.raises(StoreError):
        store.create_review(mode="bogus", source_kind="pr", source_ref="1", source_label="", source_diff="", omitted_files=[], comments=[], draft="", provider="p", model="m", status="ok", summary="", findings=[], raw="")
    assert len(store.raw_rows("reviews")) == 2
    store.clear_records()
    assert store.list_reviews() == []
    store.close()
