"""Lane assignment for the commit graph. Pure function, no git access."""

from __future__ import annotations

from dataclasses import asdict

from argonauta.gitops import GraphCommit


def assign_lanes(commits: list[GraphCommit]) -> list[dict]:
    columns: list[str | None] = []  # sha each column is waiting for
    rows: list[dict] = []

    def free_column() -> int:
        for i, s in enumerate(columns):
            if s is None:
                return i
        columns.append(None)
        return len(columns) - 1

    for commit in commits:
        waiting = [i for i, s in enumerate(columns) if s == commit.sha]
        if waiting:
            lane, merged = waiting[0], waiting[1:]
            for i in merged:
                columns[i] = None
        else:
            lane, merged = free_column(), []
        parent_lanes: list[int] = []
        if commit.parents:
            columns[lane] = commit.parents[0]
            parent_lanes.append(lane)
            for parent in commit.parents[1:]:
                existing = next((i for i, s in enumerate(columns) if s == parent), None)
                if existing is None:
                    existing = free_column()
                    columns[existing] = parent
                parent_lanes.append(existing)
        else:
            columns[lane] = None
        while columns and columns[-1] is None:
            columns.pop()
        row = asdict(commit)
        row.update(lane=lane, parent_lanes=parent_lanes, merged_lanes=merged, active=[s is not None for s in columns])
        rows.append(row)
    return rows
