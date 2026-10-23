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
    assert api.get("/app.js", token=None, origin=None, raw=True)[0] == 200
    assert api.get("/patch.js", token=None, origin=None, raw=True)[0] == 200
    assert api.get("/app.css", token=None, origin=None, raw=True)[0] == 200
    assert api.get("/vendor/xterm.js", token=None, origin=None, raw=True)[0] == 200
    status, body, headers = api.get("/logo.png", token=None, origin=None, raw=True)
    assert status == 200 and headers.get("Content-Type") == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"
    assert api.get("/favicon.ico", token=None, origin=None, raw=True)[0] == 200
    assert api.get("/../pyproject.toml", token=None, origin=None, raw=True)[0] == 404
    assert api.get("/static/../../pyproject.toml", token=None, origin=None, raw=True)[0] == 404
    for name in ("IBMPlexSans-Regular", "IBMPlexSans-Medium", "IBMPlexSans-SemiBold", "JetBrainsMono-Regular", "JetBrainsMono-Medium"):
        status, body, headers = api.get(f"/vendor/fonts/{name}.woff2", token=None, origin=None, raw=True)
        assert status == 200 and headers.get("Content-Type") == "font/woff2" and body[:4] == b"wOF2", name


def test_api_requires_token_origin_and_host(api):
    assert api.get("/api/workspaces")[0] == 200
    assert api.get("/api/workspaces", token=None)[0] == 401
    assert api.get("/api/workspaces", token="wrong")[0] == 401
    # Same-origin fetch() GETs carry no Origin header; cross-site requests always do.
    assert api.get("/api/workspaces", origin=None)[0] == 200
    assert api.get("/api/workspaces", origin="http://evil.example")[0] == 403
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


# ----- git actions (Task 8) -----------------------------------------------------
def test_stage_unstage_discard_commit(api):
    repo = api.repo
    (repo.path / "README.md").write_text("edited\n")
    (repo.path / "junk.txt").write_text("junk\n")
    assert api.post(f"/api/repos/{repo.id}/stage", {"paths": ["README.md"]})[0] == 200
    assert [e["path"] for e in api.get(f"/api/repos/{repo.id}/state")[1]["status"]["staged"]] == ["README.md"]
    assert api.post(f"/api/repos/{repo.id}/unstage", {"paths": ["README.md"]})[0] == 200
    assert api.post(f"/api/repos/{repo.id}/stage", {"paths": ["../etc"]})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/stage", {"paths": []})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/discard", {"paths": ["junk.txt"]})[0] == 200
    assert not (repo.path / "junk.txt").exists()
    api.post(f"/api/repos/{repo.id}/stage", {"paths": ["README.md"]})
    status, data = api.post(f"/api/repos/{repo.id}/commit", {"message": "from the ui"})
    assert status == 201 and len(data["sha"]) == 40
    assert git(repo.path, "log", "-1", "--format=%s").strip() == "from the ui"
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": "nothing staged"})[0] == 409


def test_commit_rejects_bad_message(api):
    repo = api.repo
    (repo.path / "README.md").write_text("edited\n")
    api.post(f"/api/repos/{repo.id}/stage", {"paths": ["README.md"]})
    head = git(repo.path, "rev-parse", "HEAD")
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": "   "})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": "x" * 5001})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/commit", {"message": 5})[0] == 400
    assert git(repo.path, "rev-parse", "HEAD") == head


def test_branch_name_validated_and_lifecycle(api):
    repo = api.repo
    assert api.post(f"/api/repos/{repo.id}/branches", {"name": "bad name"})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/branches", {"name": "a..b"})[0] == 400
    assert api.post(f"/api/repos/{repo.id}/branches", {"name": "feat/ui"})[0] == 201
    assert api.post(f"/api/repos/{repo.id}/checkout", {"name": "feat/ui"})[0] == 200
    assert api.get(f"/api/repos/{repo.id}/state")[1]["branch"] == "feat/ui"
    assert api.request("DELETE", f"/api/repos/{repo.id}/branches/feat%2Fui")[0] == 409  # current branch
    main = [b["name"] for b in api.get(f"/api/repos/{repo.id}/state")[1]["branches"] if b["name"] in ("main", "master")][0]
    api.post(f"/api/repos/{repo.id}/checkout", {"name": main})
    assert api.request("DELETE", f"/api/repos/{repo.id}/branches/feat%2Fui")[0] == 200
    assert api.post(f"/api/repos/{repo.id}/checkout", {"name": "nope"})[0] == 409


def test_fetch_pull_push_jobs(api, tmp_path):
    repo = api.repo
    bare = tmp_path / "remote.git"
    git(repo.path, "init", "--bare", "-q", str(bare))
    git(repo.path, "remote", "add", "origin", str(bare))
    status, data = api.post(f"/api/repos/{repo.id}/jobs/push", {"set_upstream": True})
    assert status == 202
    job = api.wait_job(data["job"]["id"])
    assert job["status"] == "done", job
    for kind in ("fetch", "pull"):
        status, data = api.post(f"/api/repos/{repo.id}/jobs/{kind}")
        assert status == 202 and api.wait_job(data["job"]["id"])["status"] == "done"
    state = api.get(f"/api/repos/{repo.id}/state")[1]
    assert state["ahead"] == 0 and state["upstream"].startswith("origin/")


def test_push_without_credentials_fails_fast(api, monkeypatch):
    repo = api.repo
    git(repo.path, "remote", "add", "origin", "ssh://git@127.0.0.1:9/none.git")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=2")
    status, data = api.post(f"/api/repos/{repo.id}/jobs/push", {"set_upstream": True})
    assert status == 202
    job = api.wait_job(data["job"]["id"], timeout=60)
    assert job["status"] == "failed" and job["error"]


def test_second_job_for_same_repo_is_409(api, tmp_path):
    repo = api.repo
    bare = tmp_path / "remote.git"
    git(repo.path, "init", "--bare", "-q", str(bare))
    git(repo.path, "remote", "add", "origin", str(bare))
    import threading
    gate = threading.Event()
    original = api.ctx.jobs.submit
    def slow_submit(kind, rid, fn):
        return original(kind, rid, lambda p: (gate.wait(5), fn(p))[1])
    api.ctx.jobs.submit = slow_submit
    first = api.post(f"/api/repos/{repo.id}/jobs/fetch")
    second = api.post(f"/api/repos/{repo.id}/jobs/fetch")
    gate.set()
    assert first[0] == 202 and second[0] == 409 and second[1]["job"]["id"] == first[1]["job"]["id"]
    api.wait_job(first[1]["job"]["id"])


# ----- assistant, credentials, terminals (Task 9) -------------------------------
def test_assistant_loop_over_api(api):
    repo = api.repo
    r = f"/api/repos/{repo.id}"
    assert api.get(f"{r}/proposals")[0] == 409  # not initialised
    import sys
    status, data = api.post(f"{r}/init", {"test_command": f"{sys.executable} -m pytest -q"})
    assert status == 201 and data["initialized"] is True
    assert api.get(f"{r}/settings")[1]["test_command"].endswith("pytest -q")
    assert api.request("PUT", f"{r}/settings", {"test_command": ""})[0] == 400

    status, data = api.post(f"{r}/jobs/index")
    assert status == 202 and api.wait_job(data["job"]["id"])["result"]["file_count"] >= 4

    status, data = api.post(f"{r}/jobs/propose", {"task": "Make power() handle negative exponents", "dry_run": True})
    job = api.wait_job(data["job"]["id"])
    assert job["status"] == "done" and "calc/core.py" in job["result"]["context"]

    assert api.post(f"{r}/jobs/propose", {"task": "   "})[0] == 400
    status, data = api.post(f"{r}/jobs/propose", {"task": "Make power() handle negative exponents"})
    job = api.wait_job(data["job"]["id"])
    assert job["status"] == "done" and job["result"]["proposal"]["status"] == "proposed"
    pid = job["result"]["proposal"]["id"]
    assert api.get(f"{r}/proposals/{pid}")[1]["proposal"]["id"] == pid
    assert len(api.get(f"{r}/proposals")[1]["proposals"]) == 1

    status, data = api.post(f"{r}/jobs/apply", {"proposal_id": pid, "test": True, "commit": True})
    job = api.wait_job(data["job"]["id"], timeout=180)
    assert job["status"] == "done", job
    assert job["result"]["test_run"]["status"] == "passed" and job["result"]["commit_sha"]
    assert api.get(f"{r}/proposals/{pid}")[1]["proposal"]["status"] == "applied"
    assert api.post(f"{r}/proposals/{pid}/revert")[0] == 200
    assert api.get(f"{r}/proposals/{pid}")[1]["proposal"]["status"] == "reverted"
    kinds = [e["kind"] for e in api.get(f"{r}/events")[1]["events"]]
    assert "propose" in kinds and "revert" in kinds
    assert api.post(f"{r}/proposals/999/reject")[0] == 404
    status, data = api.post(f"{r}/jobs/test")
    assert status == 202 and api.wait_job(data["job"]["id"], timeout=180)["result"]["test_run"]["status"] == "failed"


def test_propose_in_manual_mode_reports_bundle(api, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "manual")
    repo = api.repo
    r = f"/api/repos/{repo.id}"
    api.post(f"{r}/init")
    api.wait_job(api.post(f"{r}/jobs/index")[1]["job"]["id"])
    job = api.wait_job(api.post(f"{r}/jobs/propose", {"task": "anything"})[1]["job"]["id"])
    assert job["status"] == "done" and job["result"]["manual"] is True and "prompt bundle" in job["result"]["message"]


def test_credentials_endpoint(api):
    status, data = api.get(f"/api/repos/{api.repo.id}/credentials")
    assert status == 200 and set(data) == {"identity", "ssh", "signing", "head"}
    assert data["ssh"]["kind"] in ("none", "ssh", "https", "local")


def test_terminal_create_list_delete(api):
    r = f"/api/repos/{api.repo.id}"
    assert api.post(f"{r}/terminals", {"cols": "x"})[0] == 400
    status, data = api.post(f"{r}/terminals", {"cols": 80, "rows": 24})
    assert status == 201 and data["terminal"]["alive"] is True
    tid = data["terminal"]["id"]
    assert [t["id"] for t in api.get(f"{r}/terminals")[1]["terminals"]] == [tid]
    assert api.request("DELETE", f"/api/terminals/{tid}")[0] == 200
    assert api.get(f"{r}/terminals")[1]["terminals"] == []
    assert api.request("DELETE", f"/api/terminals/{tid}")[0] == 404


def test_checkout_rejects_file_paths_and_add_repo_rejects_files(api, tmp_path):
    repo = api.repo
    (repo.path / "README.md").write_text("local edit\n")
    status, data = api.post(f"/api/repos/{repo.id}/checkout", {"name": "README.md"})
    assert status == 409 and (repo.path / "README.md").read_text() == "local edit\n"
    status, data = api.post(f"/api/workspaces/{api.workspace.id}/repos", {"path": str(repo.path / "README.md")})
    assert status == 400


def test_settings_reject_unresolvable_test_command(api):
    r = f"/api/repos/{api.repo.id}"
    assert api.post(f"{r}/init", {"test_command": "definitely-not-a-real-binary -q"})[0] == 400
    api.post(f"{r}/init")
    status, data = api.request("PUT", f"{r}/settings", {"test_command": "definitely-not-a-real-binary -q"})
    assert status == 400 and "command not found" in data["error"]


def test_worktree_api_create_list_remove(api):
    r = f"/api/repos/{api.repo.id}"
    status, data = api.get(f"{r}/worktrees")
    assert status == 200 and len(data["worktrees"]) == 1 and data["worktrees"][0]["is_main"] is True
    assert api.post(f"{r}/worktrees", {"name": "bad name"})[0] == 400
    status, data = api.post(f"{r}/worktrees", {"name": "feature-a"})
    assert status == 201 and data["repo"]["parent_id"] == api.repo.id and data["repo"]["worktree"] is True
    wt_id = data["repo"]["id"]
    assert data["worktree"]["branch"] == "feature-a"
    assert any(w["path"] == data["repo"]["path"] for w in api.get(f"{r}/worktrees")[1]["worktrees"])
    # the worktree is a repository of its own: state, terminal, graph all work there
    st = api.get(f"/api/repos/{wt_id}/state")[1]
    assert st["branch"] == "feature-a" and st["repo"]["worktree"] is True
    status, t = api.post(f"/api/repos/{wt_id}/terminals", {"cols": 80, "rows": 24})
    assert status == 201 and t["terminal"]["repo_root"] == data["repo"]["path"]
    api.request("DELETE", f"/api/terminals/{t['terminal']['id']}")
    # a dirty worktree refuses removal unless forced
    from pathlib import Path
    Path(data["repo"]["path"], "dirty.txt").write_text("x\n")
    assert api.request("DELETE", f"{r}/worktrees/feature-a")[0] == 409
    assert api.request("DELETE", f"{r}/worktrees/feature-a?force=1")[0] == 200
    assert api.get(f"/api/repos/{wt_id}/state")[0] == 404  # unregistered
    assert len(api.get(f"{r}/worktrees")[1]["worktrees"]) == 1


def test_review_routes(api, monkeypatch, tmp_path):
    from tests.test_github import fake_gh
    repo = api.repo; r = f"/api/repos/{repo.id}"
    import sys
    api.post(f"{r}/init", {"test_command": ""}); api.wait_job(api.post(f"{r}/jobs/index")[1]["job"]["id"])
    monkeypatch.setenv("ARGONAUTA_SCRIPTED_DIFF", str(Path(__file__).resolve().parent.parent / "examples" / "sample_review_reply.md"))
    assert api.get("/api/github/status")[1]["installed"] in (True, False)
    assert api.get(f"{r}/github/prs")[0] == 409  # no origin
    git(repo.path, "remote", "add", "origin", "git@github.com:o/r.git")
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"pr list": '[{"number": 3, "title": "T", "author": {"login": "me"}, "headRefName": "b", "updatedAt": "x"}]', "auth status": "", "api user": '"me"'})))
    status, data = api.get(f"{r}/github/prs")
    assert status == 200 and data["origin"] == "o/r" and data["prs"][0]["number"] == 3
    assert api.get(f"{r}/state")[1]["store"]["review_sources"]["branch_base"] in ("main", "master")
    assert api.post(f"{r}/jobs/review", {"mode": "received", "source": {"kind": "worktree", "ref": ""}, "draft": ""})[0] == 400
    assert api.post(f"{r}/jobs/review", {"mode": "mine", "source": {"kind": "worktree", "ref": ""}, "draft": ""})[0] == 400
    (repo.path / "calc" / "core.py").write_text((repo.path / "calc" / "core.py").read_text() + "\n# x\n")
    status, data = api.post(f"{r}/jobs/review", {"mode": "mine", "source": {"kind": "worktree", "ref": ""}, "draft": "calc/core.py:1 fine"})
    job = api.wait_job(data["job"]["id"]); assert job["status"] == "done" and job["result"]["review"]["status"] == "ok"
    rid = job["result"]["review"]["id"]
    listed = api.get(f"{r}/reviews")[1]["reviews"]; assert listed[0]["id"] == rid and listed[0]["source_diff"] is None and listed[0]["raw"] is None
    full = api.get(f"{r}/reviews/{rid}")[1]["review"]; assert "calc/core.py" in full["source_diff"] and full["draft"].startswith("calc/core.py")
    assert api.get(f"{r}/reviews/{rid}/diff?path=calc/core.py")[1]["diff"].startswith("diff --git")
    assert api.get(f"{r}/reviews/{rid}/diff?path=nope")[0] == 404 and api.get(f"{r}/reviews/999")[0] == 404
    assert api.get(f"{r}/events")[1]["events"][0]["kind"] == "review"


def test_cached_gh_status_spawns_gh_once(monkeypatch, tmp_path):
    from argonauta.web import api as api_mod
    from tests.test_github import fake_gh
    monkeypatch.setenv("ARGONAUTA_GH_BIN", str(fake_gh(tmp_path, {"api user": '"me"'})))
    monkeypatch.setattr(api_mod, "_gh_status_cache", None)
    first = api_mod.cached_gh_status(); second = api_mod.cached_gh_status()
    assert first == second
    assert (tmp_path / "argv.txt").read_text().count("api user") == 1
