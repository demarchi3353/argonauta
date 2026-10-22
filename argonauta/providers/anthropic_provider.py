"""Real model provider using the official Anthropic SDK.

What leaves the machine: the system prompt, the task text, the repository
summary and the contents of the selected context files. Nothing else.
"""

from __future__ import annotations

from argonauta.config import Settings
from argonauta.prompts import SYSTEM_PROMPT, ProposalRequest, build_user_message
from argonauta.providers.base import ProgressCallback, ProviderError, ProviderReply, ProviderUnavailable

MAX_OUTPUT_TOKENS = 32_000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
API_BASE_URL = "https://api.anthropic.com"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise ProviderUnavailable(
                "The anthropic package is not installed. Run scripts/install.sh or "
                "`pip install 'argonauta[anthropic]'`."
            ) from exc
        if not settings.has_api_key:
            raise ProviderUnavailable("ANTHROPIC_API_KEY is not set. Add it to .env or use the manual provider.")

    def propose(self, req: ProposalRequest, on_progress: ProgressCallback | None = None) -> ProviderReply:
        import anthropic

        # The endpoint is pinned: nothing from a repository can redirect requests.
        client = anthropic.Anthropic(base_url=API_BASE_URL, max_retries=2, timeout=600)
        kwargs: dict = {
            "model": self.settings.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_message(req)}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.fallbacks == "default":
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        if on_progress:
            on_progress("contacting model")
        try:
            with client.beta.messages.stream(**kwargs) as stream:
                received = 0
                for event in stream:
                    if on_progress and getattr(event, "type", "") == "text":
                        received += len(getattr(event, "text", ""))
                        on_progress(f"receiving reply ({received} chars)")
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ProviderError("Authentication failed: the API key was rejected.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError("The API key is not allowed to use this model.") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"Model {self.settings.model!r} was not found. Check ARGONAUTA_MODEL.") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Rate limited by the API. Wait a moment and retry.") from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"The API rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"API error (HTTP {exc.status_code}). Retry later.") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach the API. Check your network connection.") from exc

        if message.stop_reason == "refusal":
            detail = ""
            details = getattr(message, "stop_details", None)
            if details is not None and getattr(details, "explanation", None):
                detail = f" ({details.explanation})"
            raise ProviderError(f"The model declined this request{detail}.")
        if message.stop_reason == "max_tokens":
            raise ProviderError("The reply was cut off at the output limit. Narrow the task and retry.")
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise ProviderError("The model returned an empty reply.")
        usage = getattr(message, "usage", None)
        return ProviderReply(
            text=text,
            model=getattr(message, "model", self.settings.model) or self.settings.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
