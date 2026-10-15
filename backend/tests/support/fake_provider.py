"""A scripted stand-in for a hosted model, and a scripted stand-in for the MCP client.

The assistant loop is tested with these, so no key, network or money is involved. A script step is one of:
a ``ProviderTurn`` (what the "model" says), an exception (what the provider raises), or a function that receives the
transcript so far and returns either of those (to react to earlier tool results like a real model would).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
from typing import Any

from app.agent.mcp_client import ToolCallResult, ToolSpec
from app.agent.provider import (
    ProviderTurn,
    StopReason,
    ToolCall,
    TranscriptEntry,
    Usage,
)


def say(text: str, *, tokens: tuple[int, int] = (10, 5), request_id: str | None = None, stop: StopReason = StopReason.END_TURN) -> ProviderTurn:
    return ProviderTurn(text=text, stop=stop, usage=Usage(*tokens), request_id=request_id)


def use(*calls: tuple[str, Any], text: str = "", tokens: tuple[int, int] = (10, 5), request_id: str | None = None) -> ProviderTurn:
    """A turn that asks for tools. Each call is ``(tool_name, arguments)``; ids are generated."""
    tool_calls = [ToolCall(id=f"call_{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls, start=1)]
    return ProviderTurn(text=text, tool_calls=tool_calls, stop=StopReason.TOOL_USE, usage=Usage(*tokens), request_id=request_id)


class ScriptedProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.transcripts: list[list[TranscriptEntry]] = []
        self.systems: list[str] = []
        self.max_tokens_seen: list[int] = []
        self.tools_seen: list[list[str]] = []

    async def complete(self, *, system: str, transcript: Sequence[TranscriptEntry], tools: Sequence[ToolSpec], max_tokens: int) -> ProviderTurn:
        self.calls += 1
        self.transcripts.append(list(transcript))
        self.systems.append(system)
        self.max_tokens_seen.append(max_tokens)
        self.tools_seen.append([t.name for t in tools])
        if not self.script:
            raise AssertionError("the scripted provider was called more times than the test expected")
        step = self.script.pop(0)
        if callable(step):
            step = step(transcript)
        if inspect.isawaitable(step):
            step = await step
        if isinstance(step, BaseException):
            raise step
        return step


class Hang:
    """A script step (or tool) that never finishes on its own and records whether it was cancelled."""

    def __init__(self) -> None:
        self.started = False
        self.was_cancelled = False

    async def __call__(self, *_: Any) -> Any:
        self.started = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise


def ok(tool: str, data: dict[str, Any], duration_ms: int = 5) -> ToolCallResult:
    return ToolCallResult(tool=tool, ok=True, data=data, duration_ms=duration_ms)


def fail(tool: str, code: str, message: str = "it failed") -> ToolCallResult:
    return ToolCallResult(tool=tool, ok=False, error_code=code, error=message, duration_ms=1)


NO_DOCUMENT_TOOLS = {"retrieve_policy_guidance"}  # like the real server: policies belong to the owner, not to one document


def spec(name: str) -> ToolSpec:
    properties: dict = {} if name in NO_DOCUMENT_TOOLS else {"document_id": {"type": "string"}}
    return ToolSpec(name=name, description=f"{name} (fake)", input_schema={"type": "object", "properties": properties})


class FakeTools:
    """Stands in for ``McpToolClient``: canned results per tool name, every call recorded."""

    def __init__(self, results: dict[str, Any], names: Sequence[str] | None = None) -> None:
        self.results = results
        self._specs = [spec(n) for n in (names or list(results))]
        self.calls: list[tuple[str, Any]] = []

    @property
    def tools(self) -> list[ToolSpec]:
        return self._specs

    async def call(self, name: str, arguments: Any) -> ToolCallResult:
        self.calls.append((name, arguments))
        outcome = self.results.get(name)
        if outcome is None:
            return fail(name, "unknown_tool", f"no tool named {name}")
        if callable(outcome):
            outcome = outcome(arguments)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome
