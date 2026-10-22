"""API tests against a real server thread, a temp repo and a bare remote."""
import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from argonauta.web.jobs import JobRunner
from argonauta.web.server import AppContext, create_server, new_token
from argonauta.web.terminal import TerminalManager
from argonauta.workspaces import open_registry
from tests.conftest import SAMPLE_PROJECT, SAMPLE_REPLY, git


class Client:
    def __init__(self, port: int, token: str):
        self.port, self.token = port, token

    def request(self, method, path, body=None, token="default", origin="default", host="default", raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {}
        if token == "default":
            token = self.token
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin == "default":
            origin = f"http://127.0.0.1:{self.port}"
        if origin is not None:
            headers["Origin"] = origin
        if host != "default":
            headers["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        if raw:
            return resp.status, payload, dict(resp.getheaders())
        try:
            return resp.status, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return resp.status, payload.decode(errors="replace")

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body or {}, **kw)

    def wait_job(self, job_id, timeout=120):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, job = self.get(f"/api/jobs/{job_id}")
            assert status == 200
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.1)
        raise AssertionError("job did not finish")


@pytest.fixture
def api(tmp_path, sample_repo, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "scripted")
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(SAMPLE_REPLY))
    registry = open_registry()
    ws = registry.add_workspace("Test")
    repo = registry.add_repo(ws.id, sample_repo)
    ctx = AppContext(registry=registry, jobs=JobRunner(), terminals=TerminalManager(), token=new_token(),
                     static_dir=Path(__file__).resolve().parent.parent / "argonauta" / "web" / "static")
    server = create_server(ctx, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(ctx.port, ctx.token)
    client.repo = repo
    client.workspace = ws
    client.ctx = ctx
    yield client
    server.shutdown()
    ctx.terminals.shutdown()
    server.server_close()


# ----- auth and static ---------------------------------------------------------
def test_index_and_static_served_without_token(api):
    status, body, headers = api.get("/", token=None, origin=None, raw=True)
    assert status == 200 and b"<!doctype html" in body.lower()
    assert "no-store" in headers.get("Cache-Control", "")
    assert api.get("/app.js", token=None, origin=None, raw=True)[0] in (200, 404)  # exists after Task 11
    assert api.get("/../pyproject.toml", token=None, origin=None, raw=True)[0] == 404
    assert api.get("/static/../../pyproject.toml", token=None, origin=None, raw=True)[0] == 404


def test_api_requires_token_origin_and_host(api):
    assert api.get("/api/workspaces")[0] == 200
    assert api.get("/api/workspaces", token=None)[0] == 401
    assert api.get("/api/workspaces", token="wrong")[0] == 401
    assert api.get("/api/workspaces", origin=None)[0] == 403
    assert api.get("/api/workspaces", host="evil.example")[0] == 403


def test_foreign_origin_rejected(api):
    status, body = api.post("/api/workspaces", {"name": "x"}, origin="http://evil.example")
    assert status == 403
    assert all(w["name"] != "x" for w in api.get("/api/workspaces")[1]["workspaces"])


def test_bad_json_and_oversized_bodies(api):
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    conn.request("POST", "/api/workspaces", body=b"{nope", headers={"Authorization": f"Bearer {api.token}", "Origin": f"http://127.0.0.1:{api.port}", "Content-Type": "application/json"})
    assert conn.getresponse().status == 400
    conn.close()
    big = {"name": "x" * (1024 * 1024 + 10)}
    assert api.post("/api/workspaces", big)[0] == 413


# ----- workspaces -------------------------------------------------------------
def test_workspace_crud(api, tmp_path):
    status, data = api.get("/api/workspaces")
    assert status == 200 and data["workspaces"][0]["repos"][0]["id"] == api.repo.id
    status, ws = api.post("/api/workspaces", {"name": "Second"})
    assert status == 201 and ws["name"] == "Second"
    assert api.request("PATCH", f"/api/workspaces/{ws['id']}", {"name": "Renamed"})[1]["name"] == "Renamed"
    assert api.post(f"/api/workspaces/{ws['id']}/repos", {"path": str(tmp_path)})[0] == 400
    other = tmp_path / "other"
    shutil.copytree(SAMPLE_PROJECT, other)
    git(other, "init", "-q")
    status, repo = api.post(f"/api/workspaces/{ws['id']}/repos", {"path": str(other)})
    assert status == 201 and repo["exists"] is True and repo["initialized"] is False
    assert api.request("DELETE", f"/api/workspaces/{ws['id']}/repos/{repo['id']}")[0] == 200
    assert api.request("DELETE", f"/api/workspaces/{ws['id']}")[0] == 200
    assert api.request("DELETE", f"/api/workspaces/{ws['id']}")[0] == 404
    assert api.post("/api/workspaces", {"name": ""})[0] == 400


# ----- read endpoints ----------------------------------------------------------
def test_state_graph_diff_and_commit(api):
    repo = api.repo
    (repo.path / "README.md").write_text("staged\n")
    git(repo.path, "add", "README.md")
    (repo.path / "calc" / "core.py").write_text("changed\n")
    (repo.path / "new.txt").write_text("new\n")
    status, state = api.get(f"/api/repos/{repo.id}/state")
    assert status == 200
    assert state["branch"] in ("main", "master") and state["head"]
    assert [e["path"] for e in state["status"]["staged"]] == ["README.md"]
    assert [e["path"] for e in state["status"]["unstaged"]] == ["calc/core.py"]
    assert [e["path"] for e in state["status"]["untracked"]] == ["new.txt"]
    assert state["store"]["initialized"] is False and state["running_job"] is None
    assert any(b["is_current"] for b in state["branches"])

    status, graph = api.get(f"/api/repos/{repo.id}/graph?limit=10")
    assert status == 200 and graph["commits"][0]["lane"] == 0 and graph["commits"][0]["subject"] == "sample project"
    sha = graph["commits"][0]["sha"]
    status, detail = api.get(f"/api/repos/{repo.id}/commits/{sha}")
    assert status == 200 and detail["commit"]["sha"] == sha and any(f["path"] == "calc/core.py" for f in detail["files"])
    assert api.get(f"/api/repos/{repo.id}/commits/notasha")[0] == 400
    assert api.get(f"/api/repos/{repo.id}/commits/{'0' * 40}")[0] == 404

    assert "+new" in api.get(f"/api/repos/{repo.id}/diff?path=new.txt&area=worktree")[1]["diff"]
    assert "+staged" in api.get(f"/api/repos/{repo.id}/diff?path=README.md&area=index")[1]["diff"]
    assert api.get(f"/api/repos/{repo.id}/diff?path=../x&area=worktree")[0] == 400
    assert api.get(f"/api/repos/{repo.id}/diff?path=.env&area=worktree")[0] == 400
    assert api.get(f"/api/repos/{repo.id}/diff?path=new.txt&area=nope")[0] == 400


def test_unknown_or_missing_repo_is_404(api, tmp_path):
    assert api.get("/api/repos/r_deadbeef/state")[0] == 404
    assert api.get("/api/repos/evil/state")[0] == 404
    other = tmp_path / "gone"
    shutil.copytree(SAMPLE_PROJECT, other)
    git(other, "init", "-q")
    status, repo = api.post(f"/api/workspaces/{api.workspace.id}/repos", {"path": str(other)})
    shutil.rmtree(other)
    assert api.get(f"/api/repos/{repo['id']}/state")[0] == 404
    assert any(r["exists"] is False for r in api.get("/api/workspaces")[1]["workspaces"][0]["repos"])


def test_unknown_job_is_404(api):
    assert api.get("/api/jobs/j_nope")[0] == 404
