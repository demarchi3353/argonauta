"""TEST-ONLY provider. Replays the file named in ARGONAUTA_SCRIPTED_DIFF.

It exists so the end-to-end test and local demos can run the full loop without
network access. `argonauta status` labels it as not a real model.
"""

from __future__ import annotations

from argonauta.config import Settings
from argonauta.prompts import ProposalRequest
from argonauta.providers.base import ProgressCallback, ProviderError, ProviderReply


class ScriptedProvider:
    name = "scripted"

    def __init__(self, settings: Settings):
        self.settings = settings

    def propose(self, req: ProposalRequest, on_progress: ProgressCallback | None = None) -> ProviderReply:
        path = self.settings.scripted_diff
        if path is None or not path.is_file():
            raise ProviderError("ARGONAUTA_SCRIPTED_DIFF must point to an existing file when the provider is 'scripted'.")
        text = path.read_text(encoding="utf-8", errors="replace")
        return ProviderReply(text=text, model="scripted", input_tokens=None, output_tokens=None)
