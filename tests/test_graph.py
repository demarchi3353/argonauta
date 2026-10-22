from argonauta.gitops import GraphCommit
from argonauta.web.graph import assign_lanes


def c(sha, parents, subject=""):
    return GraphCommit(sha=sha, short=sha[:7], parents=parents, refs=[], author="a", email="e", date="d", subject=subject or sha, signature="N")


def test_linear_history_stays_in_lane_zero():
    rows = assign_lanes([c("c3", ["c2"]), c("c2", ["c1"]), c("c1", [])])
    assert [r["lane"] for r in rows] == [0, 0, 0]
    assert rows[0]["parent_lanes"] == [0] and rows[-1]["active"] == []


def test_branch_and_merge_use_two_lanes():
    # M merges A (first parent) and B; both come from C.
    rows = assign_lanes([c("M", ["A", "B"]), c("A", ["C"]), c("B", ["C"]), c("C", [])])
    by = {r["sha"]: r for r in rows}
    assert by["M"]["lane"] == 0 and by["M"]["parent_lanes"] == [0, 1]
    assert by["A"]["lane"] == 0 and by["B"]["lane"] == 1
    assert by["C"]["lane"] == 0 and by["C"]["merged_lanes"] == [1]
    assert by["A"]["active"] == [True, True] and by["C"]["active"] == []


def test_free_columns_are_reused():
    rows = assign_lanes([c("X", ["P"]), c("Y", ["Q"]), c("P", []), c("Q", []), c("Z", [])])
    assert [r["lane"] for r in rows] == [0, 1, 0, 1, 0]
