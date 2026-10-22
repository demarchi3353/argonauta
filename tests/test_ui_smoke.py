import http.client
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import GIT_ENV


def test_ui_server_starts_serves_and_stops(tmp_path, sample_repo):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env.update(GIT_ENV)
    env["ARGONAUTA_CONFIG_DIR"] = str(tmp_path / "cfg")
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "argonauta", "--plain", "--repo", str(sample_repo), "ui", "--no-open", "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        line = ""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if "Argonauta UI:" in line or not line:
                break
        m = re.search(r"http://127\.0\.0\.1:(\d+)/#token=([A-Za-z0-9_\-]+)", line)
        assert m, f"no URL announced: {line!r}"
        port, token = int(m.group(1)), m.group(2)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200 and b"Argonauta" in resp.read()
        conn.request("GET", "/api/workspaces", headers={"Authorization": f"Bearer {token}", "Origin": f"http://127.0.0.1:{port}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200 and sample_repo.name in body  # auto-registered in Default
        conn.close()
        assert (tmp_path / "cfg" / "workspaces.json").is_file()
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("ui did not stop on SIGINT")
    assert proc.returncode == 0


def test_ui_refuses_non_loopback_host(tmp_path, sample_repo):
    env = {**os.environ, **GIT_ENV, "ARGONAUTA_CONFIG_DIR": str(tmp_path / "cfg")}
    proc = subprocess.run([sys.executable, "-m", "argonauta", "--plain", "--repo", str(sample_repo), "ui", "--no-open", "--host", "0.0.0.0"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 2 and "127.0.0.1" in proc.stderr
