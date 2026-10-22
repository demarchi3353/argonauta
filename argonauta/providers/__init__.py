"""Model providers. `get_provider` picks one from Settings."""

from __future__ import annotations

from argonauta.config import Settings
from argonauta.providers.base import Provider, ProviderError, ProviderUnavailable, ProviderReply


def get_provider(settings: Settings) -> Provider:
    name = settings.effective_provider
    if name == "anthropic":
        from argonauta.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if name == "scripted":
        from argonauta.providers.scripted import ScriptedProvider

        return ScriptedProvider(settings)
    from argonauta.providers.manual import ManualProvider

    return ManualProvider(settings)


def describe_provider(settings: Settings) -> str:
    name = settings.effective_provider
    if name == "anthropic":
        return f"anthropic ({settings.model}, effort {settings.effort})"
    if name == "scripted":
        return "scripted (TEST ONLY: replays a diff from a file, not a real model)"
    return "manual (degraded: no API key, you paste the diff yourself)"


__all__ = ["Provider", "ProviderError", "ProviderUnavailable", "ProviderReply", "get_provider", "describe_provider"]
