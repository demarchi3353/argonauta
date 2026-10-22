"""Local model provider: any server that speaks the OpenAI-compatible chat API.

Ollama, LM Studio, llama.cpp's server and vLLM all expose
`POST <base>/chat/completions`. Only the standard library is used, so this
adds no dependency. What leaves this process is the same as for the Anthropic
provider: the system prompt, the task, the repository summary and the selected
context files, sent to ARGONAUTA_LOCAL_URL, which must be a loopback address
unless ARGONAUTA_LOCAL_ALLOW_REMOTE is set in the shell.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from argonauta.config import Settings
from argonauta.prompts import SYSTEM_PROMPT, ProposalRequest, build_user_message
from argonauta.providers.base import ProgressCallback, ProviderError, ProviderReply

REQUEST_TIMEOUT = 600
MAX_REPLY_BYTES = 8 * 1024 * 1024


class LocalProvider:
    name = "local"

    def __init__(self, settings: Settings):
        if not settings.local_model:
            raise ProviderError("ARGONAUTA_LOCAL_MODEL is not set.")
        self.settings = settings
        self.endpoint = settings.local_url + "/chat/completions"

    def propose(self, req: ProposalRequest, on_progress: ProgressCallback | None = None) -> ProviderReply:
        payload = {
            "model": self.settings.local_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(req)},
            ],
            "stream": False,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        api_key = os.environ.get("ARGONAUTA_LOCAL_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        if on_progress:
            on_progress(f"waiting for {self.settings.local_model}")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310 - scheme validated in config
                raw = response.read(MAX_REPLY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            raise ProviderError(f"The local model server answered HTTP {exc.code}{detail}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"Could not reach the local model server at {self.settings.local_url}: {reason}. Is it running?") from exc
        if len(raw) > MAX_REPLY_BYTES:
            raise ProviderError("The local model server sent a reply larger than 8 MB.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("The local model server did not return JSON; check ARGONAUTA_LOCAL_URL (it should end in /v1).") from exc
        text = _message_text(data)
        if not text.strip():
            raise ProviderError("The local model returned an empty reply.")
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        model = data.get("model") if isinstance(data, dict) and isinstance(data.get("model"), str) else self.settings.local_model
        return ProviderReply(
            text=text,
            model=model,
            input_tokens=usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None,
            output_tokens=usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None,
        )


def _message_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # some servers return content parts
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = choices[0].get("text")  # legacy completions shape
    return text if isinstance(text, str) else ""


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read(65536).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else (error if isinstance(error, str) else None)
    return f": {message[:300]}" if isinstance(message, str) and message else ""
