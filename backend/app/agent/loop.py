"""The bounded tool-calling loop (docs/phase5_design.md sections 3 and 5).

What it does: show the model the conversation and the tool list, run the tools the model asks for through the MCP
client, show the results to the model, and repeat until the model answers or a limit is reached.

What it deliberately does not know: HTTP, MongoDB, or any provider SDK. It talks to a ``LlmProvider`` and a
``ToolRunner`` (the MCP client), so every behaviour here is tested with a scripted fake provider and costs nothing.

Safety properties enforced here (the model's prompt only asks for them; this code guarantees them):

* **Bounds:** model calls, tool calls, elapsed time, output tokens and total tool-result size are capped. Hitting a
  cap ends the run with a plain explanation, never a silent stop.
* **One document:** a tool call that names a different ``document_id`` than the conversation's is refused without
  being sent to the server; a missing ``document_id`` is filled in.
* **No hidden retries of actions:** the provider adapter (not this loop) retries failed *model* requests. Tools run
  only after a successful reply, and a tool that failed is reported to the model, never silently re-run.
* **Errors become fixed sentences:** a provider failure is turned into one of a fixed set of messages (no raw
  provider text, no document text, no key).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from app.agent.mcp_client import ToolCallResult, ToolSpec
from app.agent.prompts import build_system_prompt
from app.agent.provider import (
    AssistantTurn,
    LlmProvider,
    ProviderError,
    ProviderTurn,
    StopReason,
    ToolCall,
    ToolResultItem,
    ToolResults,
    TranscriptEntry,
    UserText,
    user_message_for,
)
from app.agent.summaries import summarize_tool_call
from app.config import Settings
from app.models.conversations import RunStatus, ToolEventStatus

logger = logging.getLogger("clearcase.agent.loop")

T = TypeVar("T")

CANCELLED_TEXT = "Stopped at your request. Anything already proposed stays in the Findings panel."


@dataclass(frozen=True)
class AgentLimits:
    max_rounds: int = 8  # model calls
    max_tool_calls: int = 16
    max_seconds: float = 120.0
    max_output_tokens: int = 4096  # per model call
    max_tool_result_chars: int = 40_000  # one result; a bigger one is refused, not truncated
    max_context_chars: int = 240_000  # all tool results of one run together

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentLimits:
        return cls(
            max_rounds=settings.agent_max_rounds,
            max_tool_calls=settings.agent_max_tool_calls,
            max_seconds=float(settings.agent_max_seconds),
            max_output_tokens=settings.agent_max_output_tokens,
            max_tool_result_chars=settings.agent_max_tool_result_chars,
            max_context_chars=settings.agent_max_context_chars,
        )


class ToolRunner(Protocol):
    """What the loop needs from the MCP client (``McpToolClient`` satisfies it)."""

    @property
    def tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: Any) -> ToolCallResult: ...


@dataclass
class ToolEventDraft:
    seq: int
    tool: str
    status: ToolEventStatus
    duration_ms: int
    summary: str
    error_code: str | None = None


# Called with each tool event the moment it happens, so the interface can show progress while a request is running.
ToolEventCallback = Callable[[ToolEventDraft], Awaitable[None]]


@dataclass
class RunEvidence:
    """What the tools actually returned in this run. The citation checker (a later step) verifies against it."""

    pages: set[int] = field(default_factory=set)
    policy_passages: dict[str, str] = field(default_factory=dict)  # passage id -> text; memory only, never stored


@dataclass
class LoopResult:
    status: RunStatus = RunStatus.COMPLETED
    text: str = ""
    rounds: int = 0
    tool_calls: int = 0
    limits_hit: list[str] = field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_ids: list[str] = field(default_factory=list)
    tool_events: list[ToolEventDraft] = field(default_factory=list)
    evidence: RunEvidence = field(default_factory=RunEvidence)


async def _guarded(awaitable: Awaitable[T], *, timeout: float, cancel: asyncio.Event | None) -> tuple[str, T | None]:
    """Await something, but give up on time or when ``cancel`` is set. Returns ``("ok"|"timeout"|"cancelled", value)``.

    A provider or tool exception is re-raised to the caller. If the whole run is cancelled from outside, the
    inner work is cancelled too and the cancellation propagates.
    """
    task = asyncio.ensure_future(awaitable)
    waiters: set[asyncio.Future[Any]] = {task}
    cancel_task: asyncio.Future[Any] | None = None
    if cancel is not None:
        cancel_task = asyncio.ensure_future(cancel.wait())
        waiters.add(cancel_task)
    try:
        done, _ = await asyncio.wait(waiters, timeout=max(timeout, 0.0), return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
    if task in done:
        return "ok", task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    return ("cancelled" if cancel is not None and cancel.is_set() else "timeout"), None


async def _announce(on_event: ToolEventCallback | None, event: ToolEventDraft) -> None:
    """Tell the caller about a tool event. A failure to report progress must never fail the run itself."""
    if on_event is None:
        return
    try:
        await on_event(event)
    except Exception:  # noqa: BLE001
        logger.warning("Could not report tool progress for %s", event.tool, exc_info=True)


def _bind_document(arguments: Any, document_id: str) -> tuple[Any, bool]:
    """Pin a tool call to the conversation's document. Returns ``(arguments, allowed)``."""
    if not isinstance(arguments, dict):
        return arguments, True  # the MCP client rejects non-object arguments with a clear error
    given = arguments.get("document_id")
    if given is not None and given != document_id:
        return arguments, False
    return {**arguments, "document_id": document_id}, True


def _collect_evidence(name: str, data: dict[str, Any] | None, evidence: RunEvidence) -> None:
    if not isinstance(data, dict):
        return

    def add_pages(items: Any) -> None:
        for item in items if isinstance(items, list) else []:
            page = item.get("page_number") if isinstance(item, dict) else None
            if isinstance(page, int):
                evidence.pages.add(page)

    add_pages(data.get("occurrences"))
    add_pages(data.get("candidates"))
    add_pages(data.get("created"))
    add_pages(data.get("already_existed"))
    add_pages(data.get("unresolved_items"))
    if isinstance(data.get("page_number"), int):
        evidence.pages.add(data["page_number"])
    if name == "retrieve_policy_guidance":
        for passage in data.get("passages", []) if isinstance(data.get("passages"), list) else []:
            if isinstance(passage, dict) and isinstance(passage.get("citation_id"), str):
                evidence.policy_passages[passage["citation_id"]] = str(passage.get("text", ""))


def failure_result(error: ProviderError) -> LoopResult:
    """A finished, failed result for a problem that happened before or outside the loop (for example setup)."""
    result = LoopResult()
    AgentLoop._fail(result, error)
    return result


@dataclass
class ExecutedTool:
    item: ToolResultItem  # what a model is shown for this call
    result: ToolCallResult  # the raw outcome (``data`` is set when the call succeeded)
    visible: bool  # False when a too-large result was refused: its data must not be read, cited or counted as evidence


class ToolExecutor:
    """Runs one tool call safely. Shared by the live loop and the scripted demo assistant, so both get the same rules:
    the call is pinned to the conversation's document, bounded in time, logged as a sanitized event, and only results
    that were actually shown count as evidence for citations."""

    def __init__(self, tools: ToolRunner, limits: AgentLimits) -> None:
        self._tools = tools
        self._limits = limits

    async def execute(
        self,
        call: ToolCall,
        document_id: str,
        result: LoopResult,
        time_left: float,
        cancel: asyncio.Event | None,
        on_event: ToolEventCallback | None = None,
    ) -> tuple[str, ExecutedTool | None]:
        """Returns ``("ok", executed)``, or ``("timeout" | "cancelled", None)`` if the call was stopped."""
        limits = self._limits
        result.tool_calls += 1
        seq = len(result.tool_events) + 1
        started = time.monotonic()

        # Only tools that work on a document take a document_id (the policy tool searches the owner's policies, not
        # one document), so only those are pinned to this conversation's document.
        takes_document = any(
            spec.name == call.name and "document_id" in spec.input_schema.get("properties", {}) for spec in self._tools.tools
        )
        arguments, allowed = _bind_document(call.arguments, document_id) if takes_document else (call.arguments, True)
        if not allowed:
            tool_result = ToolCallResult(
                tool=call.name,
                ok=False,
                error_code="wrong_document",
                error="This conversation is about one document only. Use the document_id given in your instructions.",
            )
        else:
            outcome, value = await _guarded(self._tools.call(call.name, arguments), timeout=time_left, cancel=cancel)
            if outcome != "ok" or value is None:
                stopped = ToolEventDraft(seq, call.name, ToolEventStatus.ERROR, int((time.monotonic() - started) * 1000), "stopped", outcome)
                result.tool_events.append(stopped)
                await _announce(on_event, stopped)
                return outcome, None
            tool_result = value

        content, is_error, error_code = self._model_view(tool_result, limits)
        visible = tool_result.ok and not is_error
        if visible:  # only what was actually shown counts as evidence
            _collect_evidence(call.name, tool_result.data, result.evidence)
        event = ToolEventDraft(
            seq=seq,
            tool=call.name,
            status=ToolEventStatus.ERROR if is_error else ToolEventStatus.OK,
            duration_ms=tool_result.duration_ms or int((time.monotonic() - started) * 1000),
            summary="too large to read" if error_code == "result_too_large" else summarize_tool_call(call.name, tool_result),
            error_code=error_code,
        )
        result.tool_events.append(event)
        await _announce(on_event, event)
        return "ok", ExecutedTool(ToolResultItem(call_id=call.id, content=content, is_error=is_error), tool_result, visible)

    @staticmethod
    def _model_view(tool_result: ToolCallResult, limits: AgentLimits) -> tuple[str, bool, str | None]:
        """What the model is shown for a tool result: ``(content, is_error, error_code)``."""
        if not tool_result.ok:
            return f"Tool error ({tool_result.error_code}): {tool_result.error}", True, tool_result.error_code
        content = json.dumps(tool_result.data, ensure_ascii=False, default=str)
        if len(content) > limits.max_tool_result_chars:
            note = (
                f"The result was too large to show you ({len(content)} characters). Ask for less: a narrower search, "
                "fewer categories, or a smaller excerpt."
            )
            return note, True, "result_too_large"
        return content, False, None


class AgentLoop:
    def __init__(self, provider: LlmProvider, tools: ToolRunner, limits: AgentLimits | None = None) -> None:
        self._provider = provider
        self._tools = tools
        self._limits = limits or AgentLimits()
        self._executor = ToolExecutor(tools, self._limits)

    async def run(
        self,
        *,
        document_id: str,
        history: Sequence[tuple[str, str]],
        user_text: str,
        cancel: asyncio.Event | None = None,
        on_tool_event: ToolEventCallback | None = None,
    ) -> LoopResult:
        """Run one request. ``history`` is ``(role, text)`` of earlier messages, oldest first, text only."""
        limits = self._limits
        started = time.monotonic()
        result = LoopResult()
        system = build_system_prompt(document_id)
        tool_specs = list(self._tools.tools)  # fixed for the whole run
        transcript: list[TranscriptEntry] = [
            UserText(text) if role == "user" else AssistantTurn(ProviderTurn(text=text)) for role, text in history
        ]
        transcript.append(UserText(user_text))
        last_text = ""
        context_chars = 0

        def remaining() -> float:
            return limits.max_seconds - (time.monotonic() - started)

        def hit(name: str) -> None:
            if name not in result.limits_hit:
                result.limits_hit.append(name)

        try:
            while True:
                if cancel is not None and cancel.is_set():
                    result.status = RunStatus.CANCELLED
                    break
                if result.rounds >= limits.max_rounds:
                    hit("max_rounds")
                    break
                if remaining() <= 0:
                    hit("max_seconds")
                    break

                outcome, turn = await _guarded(
                    self._provider.complete(
                        system=system, transcript=transcript, tools=tool_specs, max_tokens=limits.max_output_tokens
                    ),
                    timeout=remaining(),
                    cancel=cancel,
                )
                if outcome == "cancelled":
                    result.status = RunStatus.CANCELLED
                    break
                if outcome == "timeout" or turn is None:
                    hit("max_seconds")
                    break

                result.rounds += 1
                result.input_tokens += turn.usage.input_tokens
                result.output_tokens += turn.usage.output_tokens
                result.cache_read_tokens += turn.usage.cache_read_tokens
                result.cache_write_tokens += turn.usage.cache_write_tokens
                if turn.request_id:
                    result.request_ids.append(turn.request_id)
                if turn.text.strip():
                    last_text = turn.text

                if turn.stop is StopReason.REFUSAL:
                    self._fail(result, ProviderError("refusal"))
                    break
                if turn.stop is StopReason.MAX_TOKENS:
                    hit("max_output_tokens")
                    break
                if not turn.tool_calls:
                    break

                transcript.append(AssistantTurn(turn))
                items: list[ToolResultItem] = []
                stop_after_batch = False
                for call in turn.tool_calls:
                    if result.tool_calls >= limits.max_tool_calls:
                        hit("max_tool_calls")
                        stop_after_batch = True
                        break
                    if context_chars >= limits.max_context_chars:
                        hit("max_context")
                        stop_after_batch = True
                        break
                    outcome_tool, executed = await self._executor.execute(call, document_id, result, remaining(), cancel, on_tool_event)
                    if outcome_tool == "cancelled":
                        result.status = RunStatus.CANCELLED
                        stop_after_batch = True
                        break
                    if outcome_tool == "timeout" or executed is None:
                        hit("max_seconds")
                        stop_after_batch = True
                        break
                    context_chars += len(executed.item.content)
                    items.append(executed.item)
                if stop_after_batch:
                    break
                transcript.append(ToolResults(items))
        except ProviderError as exc:
            self._fail(result, exc)
        except Exception:  # noqa: BLE001 - never let an unexpected bug escape without a clean, honest result
            logger.exception("Assistant loop failed unexpectedly")
            self._fail(result, ProviderError("internal_error"))

        if result.status is RunStatus.CANCELLED:
            result.text = CANCELLED_TEXT
        elif result.status is RunStatus.COMPLETED:
            result.text = self._compose_text(last_text, result.limits_hit)
        return result

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _fail(result: LoopResult, error: ProviderError) -> None:
        result.status = RunStatus.FAILED
        result.error_code = error.code
        result.error = user_message_for(error)
        result.text = result.error
        if error.request_id:
            result.request_ids.append(error.request_id)

    def _compose_text(self, last_text: str, limits_hit: list[str]) -> str:
        limits = self._limits
        sentences = {
            "max_rounds": f"I stopped because I reached the limit of {limits.max_rounds} steps.",
            "max_tool_calls": f"I stopped because I reached the limit of {limits.max_tool_calls} tool calls.",
            "max_seconds": f"I stopped because I reached the time limit of {int(limits.max_seconds)} seconds.",
            "max_context": "I stopped because I had gathered as much document text as I am allowed to read at once.",
            "max_output_tokens": "My answer was cut off by the length limit.",
        }
        notes = [sentences[name] for name in limits_hit if name in sentences]
        if not notes:
            return last_text or "I have nothing to add."
        closing = "Anything I proposed is in the Findings panel for you to review; nothing was approved or exported."
        notice = " ".join(notes) + " " + closing
        return f"{last_text}\n\n{notice}" if last_text else notice
