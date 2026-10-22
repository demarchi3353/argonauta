from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from argonauta.prompts import ProposalRequest


class ProviderError(RuntimeError):
    """The provider could not produce a reply. Message is safe to show."""


class ProviderUnavailable(ProviderError):
    """Degraded mode: no model can be called with the current configuration."""


@dataclass(frozen=True)
class ProviderReply:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


ProgressCallback = Callable[[str], None]


class Provider(Protocol):
    name: str

    def propose(self, req: ProposalRequest, on_progress: ProgressCallback | None = None) -> ProviderReply: ...
