"""Degraded provider: no API access. Writes a prompt bundle for the user."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from argonauta.config import Settings
from argonauta.prompts import ProposalRequest, ReviewRequest, prompt_bundle, review_bundle
from argonauta.providers.base import ProgressCallback, ProviderReply, ProviderUnavailable


class ManualProvider:
    name = "manual"

    def __init__(self, settings: Settings):
        self.settings = settings

    def write_bundle(self, content: str, suffix: str = "prompt") -> Path:
        self.settings.prompts_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.settings.prompts_dir / f"{stamp}-{suffix}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def propose(self, req: ProposalRequest, on_progress: ProgressCallback | None = None) -> ProviderReply:
        path = self.write_bundle(prompt_bundle(req))
        raise ProviderUnavailable(
            "No model is configured (ANTHROPIC_API_KEY is unset), so Argonauta is in manual mode.\n"
            f"A prompt bundle was written to {path}.\n"
            "Paste it into any model, save the reply to a file, then run:\n"
            f'  argonauta propose "{req.task}" --from-diff <reply-file>'
        )

    def review(self, req: ReviewRequest, on_progress: ProgressCallback | None = None) -> ProviderReply:
        path = self.write_bundle(review_bundle(req), "review")
        raise ProviderUnavailable(
            "No model is configured (ANTHROPIC_API_KEY is unset), so Argonauta is in manual mode.\n"
            f"A review bundle was written to {path}.\n"
            "Review replies cannot be pasted back in this version; configure a provider to review."
        )
