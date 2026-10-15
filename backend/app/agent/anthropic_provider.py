"""The Anthropic adapter: turns the loop's provider-neutral transcript into Messages API calls.

This is the only module that imports the ``anthropic`` SDK for conversations. Choices that matter, all taken from the
SDK/API documentation current when this was written (re-check before changing):

* The API key comes from ``Settings`` only (never the environment variable the SDK would also read) and is never
  logged, stored or put in an error.
* The SDK retries connection errors, 408/409/429 and 5xx twice with backoff, so this adapter does not add its own
  retries. When they run out, the failure becomes a ``ProviderError`` with a stable code.
* Thinking cannot be switched off on Opus 5.5 (a 400), so the request omits ``thinking`` and sets
  ``output_config.effort``. Thinking blocks come back inside the reply and must be sent back unchanged while a run
  continues: they ride along in ``ProviderTurn.raw`` and are dropped when the run ends.
* ``tool_choice`` stays automatic (forcing a tool is not supported on this model); the prompt steers instead.
* Top-level ``cache_control`` asks the API to cache the longest stable prefix (system prompt, tools and earlier turns),
  so later rounds of a run re-read it at a fraction of the price. Cache counts are reported back for honest costing.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import anthropic

from app.agent.mcp_client import ToolSpec
from app.agent.provider import (
    AssistantTurn,
    ProviderError,
    ProviderTurn,
    StopReason,
    ToolCall,
    ToolResults,
    TranscriptEntry,
    Usage,
    UserText,
)
from app.config import Settings

logger = logging.getLogger("clearcase.agent.anthropic")

REQUEST_TIMEOUT_SECONDS = 90.0
CONNECT_TIMEOUT_SECONDS = 10.0

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}


def build_client(settings: Settings, *, max_retries: int = 2) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=settings.llm_api_key,
        max_retries=max_retries,
        timeout=anthropic.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
    )


def _tool_param(spec: ToolSpec) -> dict[str, Any]:
    return {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}


def _messages(transcript: Sequence[TranscriptEntry]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in transcript:
        if isinstance(entry, UserText):
            messages.append({"role": "user", "content": entry.text})
        elif isinstance(entry, AssistantTurn):
            if entry.turn.raw is not None:
                messages.append({"role": "assistant", "content": entry.turn.raw})  # exactly what the model sent
            elif entry.turn.text.strip():  # an empty text block is rejected by the API, so skip it
                messages.append({"role": "assistant", "content": entry.turn.text})
        elif isinstance(entry, ToolResults):
            blocks = []
            for item in entry.results:
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": item.call_id, "content": item.content}
                if item.is_error:
                    block["is_error"] = True
                blocks.append(block)
            messages.append({"role": "user", "content": blocks})
    return messages


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None


def _map_error(exc: Exception) -> ProviderError:
    """Turn an SDK exception into a ``ProviderError``. The exception's text is deliberately not carried over."""
    request_id = getattr(exc, "request_id", None)
    if isinstance(exc, anthropic.APITimeoutError):
        code = "timeout"
    elif isinstance(exc, anthropic.APIConnectionError):
        code = "connection_failed"
    elif isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status in (401, 403):
            code = "auth_failed"
        elif status == 402:
            code = "billing"
        elif status == 404:
            code = "model_not_found"
        elif status == 429:
            return ProviderError("rate_limited", "429", retry_after_seconds=_retry_after(exc), request_id=request_id)
        elif status >= 500:  # includes 529 "overloaded"
            code = "provider_unavailable"
        elif "credit balance" in str(getattr(exc, "message", "")).lower():
            code = "billing"  # the API reports an empty balance as a 400
        else:
            code = "bad_request"
    else:
        code = "internal_error"
    status_note = getattr(exc, "status_code", "-")
    return ProviderError(code, f"{type(exc).__name__} status={status_note}", request_id=request_id)


def _to_turn(response: Any) -> ProviderTurn:
    texts = [block.text for block in response.content if block.type == "text"]
    calls = [
        ToolCall(id=block.id, name=block.name, arguments=block.input) for block in response.content if block.type == "tool_use"
    ]
    usage = response.usage
    return ProviderTurn(
        text="\n\n".join(texts),
        tool_calls=calls,
        stop=_STOP_REASONS.get(response.stop_reason or "", StopReason.OTHER),
        usage=Usage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        ),
        request_id=getattr(response, "_request_id", None),
        raw=list(response.content),
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        if not settings.llm_api_key.strip() or not settings.llm_model.strip():
            raise ProviderError("not_configured")
        self.model = settings.llm_model.strip()
        self._effort = settings.agent_effort
        self._client = client or build_client(settings)

    async def complete(
        self,
        *,
        system: str,
        transcript: Sequence[TranscriptEntry],
        tools: Sequence[ToolSpec],
        max_tokens: int,
    ) -> ProviderTurn:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=_messages(transcript),
                tools=[_tool_param(t) for t in tools],
                output_config={"effort": self._effort},
                cache_control={"type": "ephemeral"},
            )
        except anthropic.AnthropicError as exc:
            error = _map_error(exc)
            logger.warning("Anthropic request failed: code=%s request_id=%s (%s)", error.code, error.request_id, error)
            raise error from None  # the SDK exception's text may echo parts of the request; do not chain it
        return _to_turn(response)


def create_provider(settings: Settings):
    """The provider chosen by ``CLEARCASE_LLM_PROVIDER``. Raises ``ProviderError('not_configured')`` if unusable."""
    if settings.llm_provider.strip().lower() == "anthropic":
        return AnthropicProvider(settings)
    raise ProviderError("not_configured", f"unsupported provider {settings.llm_provider!r}")
