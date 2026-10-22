"""The `local` provider speaks the OpenAI-compatible chat API that Ollama,
LM Studio, llama.cpp server and vLLM expose. Tested against a fake server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from argonauta.config import ConfigError, load_settings
from argonauta.prompts import ContextFile, ProposalRequest
from argonauta.providers import describe_provider, get_provider
from argonauta.providers.base import ProviderError

REPLY = "Here is the fix.\n\n```diff\ndiff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n```\n"


class FakeServer:
    def __init__(self, status=200, body=None):
        self.requests = []
        outer = self
        body = body if body is not None else {
            "id": "chatcmpl-1", "model": "qwen2.5-coder:7b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 321, "completion_tokens": 45},
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(self.rfile.read(length))})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    s = FakeServer()
    yield s
    s.stop()


def _req():
    return ProposalRequest(task="rename x to y", repo_summary="root: demo", files=[ContextFile("a.py", "x\n")])


def test_local_provider_sends_openai_chat_request_and_parses_reply(fake, sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "local")
    monkeypatch.setenv("ARGONAUTA_LOCAL_URL", fake.url)
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setenv("ARGONAUTA_LOCAL_API_KEY", "local-secret")
    settings = load_settings(sample_repo)
    assert settings.effective_provider == "local"
    assert "local" in describe_provider(settings) and "qwen2.5-coder:7b" in describe_provider(settings) and "127.0.0.1" in describe_provider(settings)
    provider = get_provider(settings)
    reply = provider.propose(_req(), on_progress=lambda m: None)
    assert reply.text == REPLY and reply.model == "qwen2.5-coder:7b"
    assert reply.input_tokens == 321 and reply.output_tokens == 45
    sent = fake.requests[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"].get("Authorization") == "Bearer local-secret"
    assert sent["body"]["model"] == "qwen2.5-coder:7b" and sent["body"]["stream"] is False
    assert sent["body"]["messages"][0]["role"] == "system" and "unified diff" in sent["body"]["messages"][0]["content"]
    assert sent["body"]["messages"][1]["role"] == "user" and "rename x to y" in sent["body"]["messages"][1]["content"] and "a.py" in sent["body"]["messages"][1]["content"]


def test_local_provider_without_key_sends_no_authorization(fake, sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "local")
    monkeypatch.setenv("ARGONAUTA_LOCAL_URL", fake.url)
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "qwen")
    get_provider(load_settings(sample_repo)).propose(_req())
    assert "Authorization" not in fake.requests[0]["headers"]


def test_local_provider_errors_are_messages(sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "local")
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "qwen")
    monkeypatch.setenv("ARGONAUTA_LOCAL_URL", "http://127.0.0.1:9/v1")  # nothing listens here
    with pytest.raises(ProviderError, match="reach"):
        get_provider(load_settings(sample_repo)).propose(_req())
    broken = FakeServer(status=500, body={"error": {"message": "model not loaded"}})
    try:
        monkeypatch.setenv("ARGONAUTA_LOCAL_URL", broken.url)
        with pytest.raises(ProviderError, match="500"):
            get_provider(load_settings(sample_repo)).propose(_req())
    finally:
        broken.stop()
    empty = FakeServer(body={"choices": [{"message": {"content": ""}}]})
    try:
        monkeypatch.setenv("ARGONAUTA_LOCAL_URL", empty.url)
        with pytest.raises(ProviderError, match="empty"):
            get_provider(load_settings(sample_repo)).propose(_req())
    finally:
        empty.stop()


def test_local_url_must_be_loopback_unless_allowed(sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "local")
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "qwen")
    monkeypatch.setenv("ARGONAUTA_LOCAL_URL", "http://models.example.com/v1")
    with pytest.raises(ConfigError, match="loopback"):
        load_settings(sample_repo)
    monkeypatch.setenv("ARGONAUTA_LOCAL_ALLOW_REMOTE", "1")
    assert load_settings(sample_repo).local_url == "http://models.example.com/v1"
    monkeypatch.setenv("ARGONAUTA_LOCAL_URL", "ftp://127.0.0.1/v1")
    with pytest.raises(ConfigError, match="http"):
        load_settings(sample_repo)


def test_local_provider_requires_a_model_and_auto_picks_it_without_a_key(sample_repo, clean_env, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_PROVIDER", "local")
    with pytest.raises(ConfigError, match="ARGONAUTA_LOCAL_MODEL"):
        load_settings(sample_repo)
    monkeypatch.delenv("ARGONAUTA_PROVIDER")
    monkeypatch.setenv("ARGONAUTA_LOCAL_MODEL", "qwen")
    assert load_settings(sample_repo).effective_provider == "local"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert load_settings(sample_repo).effective_provider == "anthropic"  # a key still wins in auto mode


def test_local_url_and_remote_flag_are_shell_only(tmp_path, clean_env, monkeypatch):
    import os
    from argonauta.config import load_repo_dotenv
    for name in ("ARGONAUTA_LOCAL_URL", "ARGONAUTA_LOCAL_ALLOW_REMOTE", "ARGONAUTA_LOCAL_MODEL", "ARGONAUTA_LOCAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("ARGONAUTA_LOCAL_URL=http://evil.example/v1\nARGONAUTA_LOCAL_ALLOW_REMOTE=1\nARGONAUTA_LOCAL_MODEL=qwen\nARGONAUTA_LOCAL_API_KEY=k\n")
    try:
        loaded = load_repo_dotenv(tmp_path / ".env")
        assert sorted(loaded) == ["ARGONAUTA_LOCAL_API_KEY", "ARGONAUTA_LOCAL_MODEL"]
        assert "ARGONAUTA_LOCAL_URL" not in os.environ and "ARGONAUTA_LOCAL_ALLOW_REMOTE" not in os.environ
    finally:  # load_repo_dotenv writes os.environ directly; do not leak into other tests
        for name in loaded:
            os.environ.pop(name, None)
