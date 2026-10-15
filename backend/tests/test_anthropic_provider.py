"""The Anthropic adapter, tested offline: the real SDK talks to a mock HTTP transport, so no key, network or money.

These tests pin what we send (request shape), how we read replies, how every failure is classified, and that a key
or a provider's error text never leaks. A single live call is checked separately by hand (scripts/live_check.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from app.agent.anthropic_provider import AnthropicProvider, build_client, create_provider
from app.agent.loop import AgentLimits, AgentLoop
from app.agent.mcp_client import ToolSpec
from app.agent.provider import (
    AssistantTurn,
    ProviderError,
    StopReason,
    ToolResultItem,
    ToolResults,
    UserText,
)
from app.config import Settings
from tests.support.fake_provider import FakeTools, ok

KEY = "sk-ant-test-0123456789-SECRET"
ECHO = "ECHOED-DOCUMENT-TEXT Jane Q. Sentinel-Smith"  # what a provider error body might echo back from a request

TOOL = ToolSpec(
    name="search_document",
    description="Search the document.",
    input_schema={"type": "object", "properties": {"document_id": {"type": "string"}, "query": {"type": "string"}}, "required": ["document_id", "query"]},
)


def reply(content: list[dict[str, Any]], *, stop: str = "end_turn", usage: dict[str, int] | None = None, request_id: str = "req_test_1") -> httpx2.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 12, "output_tokens": 4},
    }
    return httpx2.Response(200, json=body, headers={"request-id": request_id})


def error_reply(status: int, message: str = "nope", *, headers: dict[str, str] | None = None, request_id: str = "req_err_1") -> httpx2.Response:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return httpx2.Response(status, json=body, headers={"request-id": request_id, **(headers or {})})


class Recorder:
    """A mock transport handler that records requests and answers from a queue."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def provider_for(handler: Callable[[httpx2.Request], httpx2.Response], *, key: str = KEY, model: str = "claude-opus-5-5", effort: str = "medium") -> AnthropicProvider:
    settings = Settings(_env_file=None, llm_provider="anthropic", llm_api_key=key, llm_model=model, agent_effort=effort)
    client = anthropic.AsyncAnthropic(
        api_key=key, max_retries=0, http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )
    return AnthropicProvider(settings, client=client)


def complete(provider: AnthropicProvider, transcript, *, tools=(TOOL,), max_tokens: int = 1234, system: str = "SYSTEM PROMPT"):
    return asyncio.run(provider.complete(system=system, transcript=transcript, tools=list(tools), max_tokens=max_tokens))


# ----------------------------------------------------------------------------- what we send
def test_the_request_has_the_documented_shape_and_uses_the_settings_key():
    rec = Recorder(reply([{"type": "text", "text": "hi"}]))
    complete(provider_for(rec, effort="low"), [UserText("find names")])

    request, body = rec.requests[0], rec.body()
    assert request.url.path == "/v1/messages" and request.headers["x-api-key"] == KEY
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 1234 and body["system"] == "SYSTEM PROMPT"
    assert body["tools"] == [{"name": "search_document", "description": "Search the document.", "input_schema": TOOL.input_schema}]
    assert body["output_config"] == {"effort": "low"} and body["cache_control"] == {"type": "ephemeral"}
    assert body["messages"] == [{"role": "user", "content": "find names"}]
    # not allowed / not wanted on this model: switching thinking off, or forcing a tool
    assert "thinking" not in body and "tool_choice" not in body


def test_a_transcript_with_history_tool_calls_and_tool_errors_maps_to_api_messages():
    first = reply(
        [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "toolu_1", "name": "search_document", "input": {"document_id": "d", "query": "Jane"}}],
        stop="tool_use",
    )
    rec = Recorder(first, reply([{"type": "text", "text": "done"}]))
    provider = provider_for(rec)
    turn = complete(provider, [UserText("earlier question"), AssistantTurn(_text_turn("earlier answer")), UserText("find Jane")])
    transcript = [
        UserText("find Jane"),
        AssistantTurn(turn),
        ToolResults([ToolResultItem("toolu_1", "{\"returned\": 1}"), ToolResultItem("toolu_2", "Tool error (x): nope", is_error=True)]),
    ]
    complete(provider, transcript)
    messages = rec.body()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"returned": 1}'},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "Tool error (x): nope", "is_error": True},
    ]
    # the first request carried history as plain text turns
    assert rec.body(0)["messages"] == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "find Jane"},
    ]


def test_an_empty_assistant_text_is_left_out_because_the_api_rejects_empty_blocks():
    rec = Recorder(reply([{"type": "text", "text": "ok"}]))
    complete(provider_for(rec), [UserText("a"), AssistantTurn(_text_turn("   ")), UserText("b")])
    assert [m["role"] for m in rec.body()["messages"]] == ["user", "user"]


def _text_turn(text: str):
    from app.agent.provider import ProviderTurn

    return ProviderTurn(text=text)


# ----------------------------------------------------------------------------- what we read
def test_a_reply_with_thinking_text_and_a_tool_call_is_read_correctly():
    rec = Recorder(
        reply(
            [
                {"type": "thinking", "thinking": "", "signature": "SIG-ABC"},
                {"type": "text", "text": "I will search."},
                {"type": "tool_use", "id": "toolu_9", "name": "search_document", "input": {"document_id": "d", "query": "Jane"}},
            ],
            stop="tool_use",
            usage={"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 300},
            request_id="req_abc",
        )
    )
    turn = complete(provider_for(rec), [UserText("x")])
    assert turn.text == "I will search." and turn.stop is StopReason.TOOL_USE
    assert [(c.id, c.name, c.arguments) for c in turn.tool_calls] == [("toolu_9", "search_document", {"document_id": "d", "query": "Jane"})]
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.cache_read_tokens, turn.usage.cache_write_tokens) == (100, 40, 2000, 300)
    assert turn.request_id == "req_abc"
    assert [b.type for b in turn.raw] == ["thinking", "text", "tool_use"]


def test_thinking_blocks_are_sent_back_unchanged_while_the_run_continues():
    rec = Recorder(
        reply([{"type": "thinking", "thinking": "", "signature": "SIG-ABC"}, {"type": "tool_use", "id": "toolu_1", "name": "search_document", "input": {"q": 1}}], stop="tool_use"),
        reply([{"type": "text", "text": "done"}]),
    )
    provider = provider_for(rec)
    first = complete(provider, [UserText("go")])
    complete(provider, [UserText("go"), AssistantTurn(first), ToolResults([ToolResultItem("toolu_1", "{}")])])
    sent = rec.body()["messages"][1]["content"]
    assert sent[0]["type"] == "thinking" and sent[0]["signature"] == "SIG-ABC"
    assert sent[1]["type"] == "tool_use" and sent[1]["id"] == "toolu_1" and sent[1]["input"] == {"q": 1}


@pytest.mark.parametrize(
    ("api_value", "expected"),
    [("end_turn", StopReason.END_TURN), ("tool_use", StopReason.TOOL_USE), ("max_tokens", StopReason.MAX_TOKENS), ("refusal", StopReason.REFUSAL), ("pause_turn", StopReason.OTHER), ("stop_sequence", StopReason.OTHER)],
)
def test_stop_reasons_are_mapped(api_value: str, expected: StopReason):
    rec = Recorder(reply([{"type": "text", "text": "x"}], stop=api_value))
    assert complete(provider_for(rec), [UserText("x")]).stop is expected


# ----------------------------------------------------------------------------- failures
@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (401, "invalid x-api-key", "auth_failed"),
        (403, "forbidden", "auth_failed"),
        (404, "model: claude-nope", "model_not_found"),
        (402, "payment required", "billing"),
        (400, "Your credit balance is too low to access the Anthropic API.", "billing"),
        (400, "messages: something is malformed", "bad_request"),
        (413, "request too large", "bad_request"),
        (500, "internal error", "provider_unavailable"),
        (529, "overloaded", "provider_unavailable"),
    ],
)
def test_http_failures_are_classified_and_never_echo_the_provider_text_or_the_key(status: int, message: str, code: str, caplog):
    rec = Recorder(error_reply(status, f"{message} {ECHO} {KEY}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as exc:
        complete(provider_for(rec), [UserText("x")])
    error = exc.value
    assert error.code == code and error.request_id == "req_err_1"
    for text in (str(error), repr(error), *(str(a) for a in error.args), caplog.text):
        assert KEY not in text and ECHO not in text and "Sentinel" not in text
    assert error.__cause__ is None and error.__suppress_context__  # the SDK exception (and its body) is not chained


def test_a_rate_limit_carries_the_retry_after_wait():
    rec = Recorder(error_reply(429, "slow down", headers={"retry-after": "30"}))
    with pytest.raises(ProviderError) as exc:
        complete(provider_for(rec), [UserText("x")])
    assert exc.value.code == "rate_limited" and exc.value.retry_after_seconds == 30.0


def test_a_rate_limit_without_a_usable_header_still_classifies():
    rec = Recorder(error_reply(429, "slow down", headers={"retry-after": "soon"}))
    with pytest.raises(ProviderError) as exc:
        complete(provider_for(rec), [UserText("x")])
    assert exc.value.code == "rate_limited" and exc.value.retry_after_seconds is None


@pytest.mark.parametrize(
    ("raised", "code"),
    [(httpx2.ConnectError("no route to host"), "connection_failed"), (httpx2.ReadTimeout("took too long"), "timeout")],
)
def test_network_failures_are_classified(raised: Exception, code: str):
    with pytest.raises(ProviderError) as exc:
        complete(provider_for(Recorder(raised)), [UserText("x")])
    assert exc.value.code == code


def test_the_sdk_retries_transient_errors_by_default_so_the_adapter_adds_none_of_its_own():
    settings = Settings(_env_file=None, llm_provider="anthropic", llm_api_key=KEY, llm_model="m")
    assert build_client(settings).max_retries == 2


# ----------------------------------------------------------------------------- setup problems
@pytest.mark.parametrize(
    "overrides",
    [{"llm_api_key": ""}, {"llm_api_key": "   "}, {"llm_model": ""}],
)
def test_missing_key_or_model_is_a_clear_setup_error_before_anything_is_sent(overrides: dict):
    base = {"llm_provider": "anthropic", "llm_api_key": KEY, "llm_model": "claude-opus-5-5"} | overrides
    with pytest.raises(ProviderError) as exc:
        create_provider(Settings(_env_file=None, **base))
    assert exc.value.code == "not_configured"


@pytest.mark.parametrize("provider", ["", "openai", "gemini"])
def test_an_unsupported_provider_name_is_a_setup_error(provider: str):
    with pytest.raises(ProviderError) as exc:
        create_provider(Settings(_env_file=None, llm_provider=provider, llm_api_key=KEY, llm_model="m"))
    assert exc.value.code == "not_configured"


def test_the_provider_name_is_case_insensitive():
    assert create_provider(Settings(_env_file=None, llm_provider=" Anthropic ", llm_api_key=KEY, llm_model="m")).name == "anthropic"


def test_the_key_comes_from_settings_not_from_the_sdks_own_environment_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-should-be-ignored")
    client = build_client(Settings(_env_file=None, llm_provider="anthropic", llm_api_key="settings-key", llm_model="m"))
    assert client.api_key == "settings-key"


# ----------------------------------------------------------------------------- adapter + loop together
def test_the_loop_drives_the_adapter_through_a_tool_round_trip():
    rec = Recorder(
        reply([{"type": "tool_use", "id": "toolu_1", "name": "search_document", "input": {"query": "Jane"}}], stop="tool_use", usage={"input_tokens": 50, "output_tokens": 10}),
        reply([{"type": "text", "text": "Found two matches on pages 2 and 3."}], usage={"input_tokens": 80, "output_tokens": 12, "cache_read_input_tokens": 40}),
    )
    tools = FakeTools({"search_document": ok("search_document", {"returned": 2, "occurrences": [{"page_number": 2}, {"page_number": 3}], "unsupported_pages": []})}, names=["search_document"])
    loop = AgentLoop(provider_for(rec), tools, AgentLimits())
    result = asyncio.run(loop.run(document_id="doc1", history=[], user_text="find Jane"))

    assert result.text == "Found two matches on pages 2 and 3." and result.rounds == 2
    assert tools.calls == [("search_document", {"query": "Jane", "document_id": "doc1"})]
    second = rec.body(1)["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result" and second[-1]["content"][0]["tool_use_id"] == "toolu_1"
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (130, 22, 40)
    assert KEY not in json.dumps(rec.body(0)) and KEY not in json.dumps(rec.body(1))  # the key is a header, never in the body


def test_a_provider_failure_inside_the_loop_becomes_the_fixed_user_message():
    rec = Recorder(error_reply(401, f"bad key {KEY} {ECHO}"))
    loop = AgentLoop(provider_for(rec), FakeTools({}), AgentLimits())
    result = asyncio.run(loop.run(document_id="doc1", history=[], user_text="x"))
    assert result.error_code == "auth_failed" and "API key" in result.text
    assert KEY not in result.text and ECHO not in result.text and result.request_ids == ["req_err_1"]
