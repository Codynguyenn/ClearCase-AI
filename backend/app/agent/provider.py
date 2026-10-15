"""The boundary between the assistant loop and a hosted model.

The loop (``loop.py``) only ever sees the small provider-neutral types below. One adapter per provider turns them
into that provider's API calls; today the only adapter is for Anthropic (added in the next step). Keeping this
boundary narrow means:

* tests drive the whole loop with a scripted fake provider, so CI never needs a key or network;
* adding or swapping a provider touches one file;
* the SDK's exception types never leak into the loop: an adapter raises ``ProviderError`` with a stable code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.agent.mcp_client import ToolSpec


class StopReason(StrEnum):
    END_TURN = "end_turn"  # the model finished its answer
    TOOL_USE = "tool_use"  # the model wants tools run before it continues
    MAX_TOKENS = "max_tokens"  # the answer was cut off by the output limit
    REFUSAL = "refusal"  # the model declined to answer
    OTHER = "other"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Any  # whatever the model sent; the MCP client validates it against the tool's schema


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # not served from the prompt cache
    output_tokens: int = 0
    cache_read_tokens: int = 0  # served from the prompt cache (billed at a fraction)
    cache_write_tokens: int = 0  # written to the prompt cache (billed at a premium)


@dataclass
class ProviderTurn:
    """One model reply."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: StopReason = StopReason.END_TURN
    usage: Usage = field(default_factory=Usage)
    request_id: str | None = None  # the provider's id for this call, kept for support questions
    # Provider-specific content (for example thinking blocks) that must be sent back unchanged while the same run
    # continues. Held in memory for the length of one run only: never stored, never shown.
    raw: Any = None


@dataclass(frozen=True)
class UserText:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    turn: ProviderTurn


@dataclass(frozen=True)
class ToolResultItem:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolResults:
    results: list[ToolResultItem]


TranscriptEntry = UserText | AssistantTurn | ToolResults


class ProviderError(Exception):
    """A provider call failed. ``code`` is stable; ``message`` is for logs and is never shown to the user as is."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retry_after_seconds: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id


# What the reviewer is told for each failure. These are fixed sentences on purpose: raw provider error bodies are
# never stored or displayed, so nothing from a request (document text) or a credential can ride along in them.
USER_MESSAGES: dict[str, str] = {
    "not_configured": (
        "Live mode is on, but the AI provider is not set up. Set CLEARCASE_LLM_PROVIDER, CLEARCASE_LLM_API_KEY and "
        "CLEARCASE_LLM_MODEL, then restart. Nothing was sent anywhere."
    ),
    "auth_failed": "The AI provider rejected the API key. It may be wrong, expired or deleted; check CLEARCASE_LLM_API_KEY.",
    "billing": "The AI provider account has no usable credit. Add credit in the provider's console and try again.",
    "rate_limited": "The AI provider is rate limiting requests. Wait a little and try again.",
    "provider_unavailable": "The AI provider is temporarily unavailable. Try again shortly.",
    "connection_failed": "Could not reach the AI provider. Check your internet connection and try again.",
    "timeout": "The AI provider took too long to answer. Try again.",
    "model_not_found": "The configured model is not available to this key. Check CLEARCASE_LLM_MODEL.",
    "bad_request": "The AI provider rejected the request. Nothing was changed.",
    "tools_unavailable": "The document tools could not be started. Nothing was changed; check the server log and try again.",
    "refusal": "The model declined to respond to this request.",
    "internal_error": "The assistant hit an unexpected error. Nothing was changed. Try again, and check the server log if it repeats.",
}


def user_message_for(error: ProviderError) -> str:
    base = USER_MESSAGES.get(error.code, USER_MESSAGES["internal_error"])
    if error.code == "rate_limited" and error.retry_after_seconds:
        base += f" (The provider suggests waiting about {int(error.retry_after_seconds)} s.)"
    return base


class LlmProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        transcript: Sequence[TranscriptEntry],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ProviderTurn:
        """Send the conversation so far and return the model's next reply. Raises ``ProviderError``."""
        ...
