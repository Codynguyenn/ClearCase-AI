"""The bounded assistant loop, driven by a scripted fake model and fake tools (no network, no key, no database)."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.agent.loop import CANCELLED_TEXT, AgentLimits, AgentLoop, LoopResult
from app.agent.prompts import build_system_prompt
from app.agent.provider import (
    USER_MESSAGES,
    AssistantTurn,
    ProviderError,
    StopReason,
    ToolResults,
    UserText,
)
from app.agent.summaries import summarize_tool_call
from app.config import Settings
from app.models.conversations import RunStatus, ToolEventStatus
from tests.support.fake_provider import FakeTools, Hang, ScriptedProvider, fail, ok, say, use

DOC = "doc1"
SECRET = "Jane Q. Sentinel-Smith, 555-0100"  # stands in for document text that must never reach stored summaries

SEARCH_RESULT = ok(
    "search_document",
    {
        "returned": 2,
        "truncated": False,
        "occurrences": [
            {"page_number": 2, "text": SECRET, "context": SECRET},
            {"page_number": 3, "text": SECRET, "context": SECRET},
        ],
        "unsupported_pages": [],
    },
)


def go(provider: ScriptedProvider, tools: FakeTools, *, limits: AgentLimits | None = None, history=(), text="find names", cancel=None, after=None) -> LoopResult:
    """Run the loop to completion. ``after`` runs inside the event loop right after the run returns.

    Checking cancellation there matters: ``asyncio.run`` cancels any task still pending when it shuts down, which
    would make "was it cancelled?" true even if the loop had abandoned the work.
    """

    async def scenario() -> LoopResult:
        result = await AgentLoop(provider, tools, limits or AgentLimits()).run(
            document_id=DOC, history=history, user_text=text, cancel=cancel
        )
        await asyncio.sleep(0)  # one scheduling turn, so a task that was cancelled can record it
        if after is not None:
            after()
        return result

    return asyncio.run(scenario())


def _assert(condition: bool, message: str) -> None:
    assert condition, message


# ----------------------------------------------------------------------------- normal runs
def test_a_plain_answer_with_no_tools():
    provider = ScriptedProvider([say("Hello", tokens=(100, 20), request_id="req_1")])
    result = go(provider, FakeTools({}))
    assert result.status is RunStatus.COMPLETED and result.text == "Hello"
    assert (result.rounds, result.tool_calls, result.input_tokens, result.output_tokens) == (1, 0, 100, 20)
    assert result.request_ids == ["req_1"] and result.tool_events == [] and result.limits_hit == []


def test_a_tool_round_trip_feeds_the_result_back_and_logs_a_safe_summary():
    provider = ScriptedProvider([use(("search_document", {"query": "Jane"})), say("Found it on pages 2 and 3.")])
    tools = FakeTools({"search_document": SEARCH_RESULT})
    result = go(provider, tools)

    assert tools.calls == [("search_document", {"query": "Jane", "document_id": DOC})]
    results = provider.transcripts[1][-1]
    assert isinstance(results, ToolResults) and results.results[0].call_id == "call_1" and not results.results[0].is_error
    assert '"returned": 2' in results.results[0].content
    (event,) = result.tool_events
    assert (event.seq, event.tool, event.status) == (1, "search_document", ToolEventStatus.OK)
    assert event.summary == "2 match(es) on page(s) 2, 3; 0 page(s) not searched"
    assert SECRET not in event.summary and "Jane" not in event.summary
    assert result.evidence.pages == {2, 3}
    assert result.text == "Found it on pages 2 and 3." and result.tool_calls == 1 and result.rounds == 2


def test_the_model_is_shown_the_same_tools_every_round_and_the_configured_output_limit():
    provider = ScriptedProvider([use(("a", {})), use(("a", {})), say("ok")])
    go(provider, FakeTools({"a": ok("a", {})}), limits=AgentLimits(max_output_tokens=777))
    assert provider.tools_seen == [["a"], ["a"], ["a"]] and provider.max_tokens_seen == [777, 777, 777]


def test_history_is_replayed_as_text_before_the_new_message():
    provider = ScriptedProvider([say("fine")])
    go(provider, FakeTools({}), history=[("user", "q1"), ("assistant", "a1")], text="q2")
    first, second, third = provider.transcripts[0]
    assert first == UserText("q1") and third == UserText("q2")
    assert isinstance(second, AssistantTurn) and second.turn.text == "a1" and second.turn.tool_calls == [] and second.turn.raw is None
    assert DOC in provider.systems[0]


# ----------------------------------------------------------------------------- one document only
def test_a_tool_call_naming_another_document_is_refused_without_reaching_the_tools():
    provider = ScriptedProvider([use(("search_document", {"document_id": "other-doc", "query": "x"})), say("sorry")])
    tools = FakeTools({"search_document": SEARCH_RESULT})
    result = go(provider, tools)
    assert tools.calls == []  # never sent
    seen = provider.transcripts[1][-1].results[0]
    assert seen.is_error and "wrong_document" in seen.content and "one document only" in seen.content
    (event,) = result.tool_events
    assert event.status is ToolEventStatus.ERROR and event.error_code == "wrong_document" and event.summary == "failed: wrong_document"


def test_the_document_id_is_filled_in_when_the_model_leaves_it_out_and_kept_when_correct():
    provider = ScriptedProvider([use(("search_document", {"query": "a"}), ("search_document", {"document_id": DOC, "query": "b"})), say("ok")])
    tools = FakeTools({"search_document": SEARCH_RESULT})
    go(provider, tools)
    assert [args["document_id"] for _, args in tools.calls] == [DOC, DOC]


def test_non_object_arguments_are_passed_on_so_the_client_can_reject_them_clearly():
    provider = ScriptedProvider([use(("search_document", "not an object")), say("ok")])
    tools = FakeTools({"search_document": fail("search_document", "invalid_arguments", "arguments must be an object")})
    result = go(provider, tools)
    assert tools.calls == [("search_document", "not an object")]
    assert result.tool_events[0].error_code == "invalid_arguments"


# ----------------------------------------------------------------------------- tool failures reach the model
def test_a_failed_tool_is_reported_to_the_model_which_can_recover():
    provider = ScriptedProvider(
        [use(("propose_redactions", {"items": []})), use(("search_document", {"query": "x"})), say("recovered")]
    )
    tools = FakeTools(
        {"propose_redactions": fail("propose_redactions", "invalid_arguments", "items: too short"), "search_document": SEARCH_RESULT}
    )
    result = go(provider, tools)
    error_seen = provider.transcripts[1][-1].results[0]
    assert error_seen.is_error and error_seen.content.startswith("Tool error (invalid_arguments)")
    assert [e.status for e in result.tool_events] == [ToolEventStatus.ERROR, ToolEventStatus.OK]
    assert result.status is RunStatus.COMPLETED and result.text == "recovered"


def test_an_unknown_tool_is_just_another_tool_error():
    provider = ScriptedProvider([use(("approve_everything", {})), say("ok")])
    tools = FakeTools({"search_document": SEARCH_RESULT})
    result = go(provider, tools)
    assert tools.calls[0][0] == "approve_everything"  # the real client answers unknown_tool; nothing can approve
    assert result.tool_events[0].error_code == "unknown_tool"


def test_an_oversized_result_is_refused_and_the_model_is_told_to_ask_for_less():
    big = ok("search_document", {"returned": 1, "occurrences": [{"page_number": 1, "text": "x" * 5000}]})
    provider = ScriptedProvider([use(("search_document", {"query": "x"})), say("ok")])
    result = go(provider, FakeTools({"search_document": big}), limits=AgentLimits(max_tool_result_chars=1000))
    seen = provider.transcripts[1][-1].results[0]
    assert seen.is_error and "too large to show you" in seen.content and "x" * 100 not in seen.content
    assert result.tool_events[0].error_code == "result_too_large" and result.tool_events[0].summary == "too large to read"
    assert result.evidence.pages == set()  # a result the model never saw is not evidence


def test_evidence_collects_pages_and_policy_passages_from_what_tools_returned():
    policy = ok(
        "retrieve_policy_guidance",
        {"status": "ok", "passages": [{"citation_id": "p:1.0:3", "text": "Names must be redacted."}]},
    )
    excerpt = ok("get_document_excerpt", {"page_number": 4, "start": 0, "end": 100, "has_more": False})
    provider = ScriptedProvider([use(("retrieve_policy_guidance", {"query": "names"}), ("get_document_excerpt", {"page_number": 4})), say("ok")])
    result = go(provider, FakeTools({"retrieve_policy_guidance": policy, "get_document_excerpt": excerpt}))
    assert result.evidence.policy_passages == {"p:1.0:3": "Names must be redacted."} and result.evidence.pages == {4}


# ----------------------------------------------------------------------------- limits
def test_the_step_limit_stops_a_model_that_keeps_asking_for_tools():
    provider = ScriptedProvider([use(("a", {}), text="Looking...") for _ in range(5)])
    result = go(provider, FakeTools({"a": ok("a", {})}), limits=AgentLimits(max_rounds=3))
    assert result.status is RunStatus.COMPLETED and result.limits_hit == ["max_rounds"] and provider.calls == 3
    assert result.text.startswith("Looking...") and "limit of 3 steps" in result.text
    assert "nothing was approved or exported" in result.text


def test_the_tool_call_limit_stops_in_the_middle_of_a_batch_and_asks_the_model_nothing_more():
    provider = ScriptedProvider([use(("a", {}), ("a", {}), ("a", {}))])
    tools = FakeTools({"a": ok("a", {})})
    result = go(provider, tools, limits=AgentLimits(max_tool_calls=2))
    assert len(tools.calls) == 2 and result.tool_calls == 2 and result.limits_hit == ["max_tool_calls"] and provider.calls == 1
    assert "limit of 2 tool calls" in result.text


def test_the_context_budget_stops_further_tool_calls():
    provider = ScriptedProvider([use(("a", {}), ("a", {}), ("a", {}))])
    tools = FakeTools({"a": ok("a", {"pad": "y" * 60})})
    result = go(provider, tools, limits=AgentLimits(max_context_chars=100))
    assert len(tools.calls) == 2 and result.limits_hit == ["max_context"] and "as much document text" in result.text


def test_the_time_limit_cancels_a_model_call_that_never_returns():
    hang = Hang()
    provider = ScriptedProvider([hang])
    started = time.monotonic()
    result = go(provider, FakeTools({}), limits=AgentLimits(max_seconds=0.2), after=lambda: _assert(hang.was_cancelled, "model call left running"))
    assert time.monotonic() - started < 3
    assert hang.started and hang.was_cancelled
    assert result.status is RunStatus.COMPLETED and result.limits_hit == ["max_seconds"] and "time limit" in result.text


def test_the_time_limit_cancels_a_tool_that_never_returns():
    hang = Hang()
    provider = ScriptedProvider([use(("a", {}))])
    result = go(provider, FakeTools({"a": hang}), limits=AgentLimits(max_seconds=0.2), after=lambda: _assert(hang.was_cancelled, "tool call left running"))
    assert hang.was_cancelled and result.limits_hit == ["max_seconds"] and provider.calls == 1
    assert result.tool_events[-1].status is ToolEventStatus.ERROR and result.tool_events[-1].error_code == "timeout"


def test_an_answer_cut_off_by_the_output_limit_is_kept_and_labelled():
    provider = ScriptedProvider([say("The names are Jane and", stop=StopReason.MAX_TOKENS)])
    result = go(provider, FakeTools({}))
    assert result.status is RunStatus.COMPLETED and result.limits_hit == ["max_output_tokens"]
    assert result.text.startswith("The names are Jane and") and "cut off" in result.text


# ----------------------------------------------------------------------------- provider failures
@pytest.mark.parametrize("code", [c for c in USER_MESSAGES if c not in {"refusal", "internal_error"}])
def test_every_provider_failure_becomes_a_fixed_sentence_with_no_raw_text(code: str):
    leak = "RAW-BODY sk-ant-api03-SECRET and document text Jane Smith"
    provider = ScriptedProvider([ProviderError(code, leak, request_id="req_9")])
    tools = FakeTools({"a": ok("a", {})})
    result = go(provider, tools)
    assert result.status is RunStatus.FAILED and result.error_code == code
    assert result.error == USER_MESSAGES[code] == result.text
    assert "sk-ant" not in result.text and "Jane" not in result.text and "RAW-BODY" not in result.text
    assert result.request_ids == ["req_9"] and tools.calls == []


def test_a_rate_limit_message_includes_the_providers_suggested_wait():
    provider = ScriptedProvider([ProviderError("rate_limited", retry_after_seconds=30)])
    assert "about 30 s" in go(provider, FakeTools({})).text


def test_a_model_refusal_is_reported_as_a_refusal_not_an_empty_answer():
    result = go(ScriptedProvider([say("", stop=StopReason.REFUSAL)]), FakeTools({}))
    assert result.status is RunStatus.FAILED and result.error_code == "refusal" and result.text == USER_MESSAGES["refusal"]


def test_an_unexpected_bug_becomes_a_clean_failure_that_does_not_echo_the_exception():
    provider = ScriptedProvider([RuntimeError("boom with secret sk-ant-123 and Jane Smith")])
    result = go(provider, FakeTools({}))
    assert result.status is RunStatus.FAILED and result.error_code == "internal_error"
    assert "boom" not in result.text and "sk-ant" not in result.text


def test_a_failure_after_some_tool_work_keeps_what_was_logged():
    provider = ScriptedProvider([use(("search_document", {"query": "x"})), ProviderError("provider_unavailable")])
    result = go(provider, FakeTools({"search_document": SEARCH_RESULT}))
    assert result.status is RunStatus.FAILED and result.error_code == "provider_unavailable"
    assert len(result.tool_events) == 1 and result.tool_calls == 1 and result.rounds == 1


# ----------------------------------------------------------------------------- cancellation
def test_cancelling_during_a_model_call_stops_it_promptly():
    hang = Hang()
    provider = ScriptedProvider([hang])
    cancel = asyncio.Event()

    async def scenario() -> LoopResult:
        loop = AgentLoop(provider, FakeTools({}), AgentLimits())
        task = asyncio.create_task(loop.run(document_id=DOC, history=[], user_text="x", cancel=cancel))
        await asyncio.sleep(0.1)
        cancel.set()
        outcome = await asyncio.wait_for(task, timeout=3)
        await asyncio.sleep(0)
        assert hang.was_cancelled, "the model call was left running"
        return outcome

    result = asyncio.run(scenario())
    assert hang.was_cancelled and result.status is RunStatus.CANCELLED and result.text == CANCELLED_TEXT


def test_cancelling_during_a_tool_call_stops_it_and_records_the_event():
    hang = Hang()
    provider = ScriptedProvider([use(("a", {}))])
    tools = FakeTools({"a": hang})
    cancel = asyncio.Event()

    async def scenario() -> LoopResult:
        loop = AgentLoop(provider, tools, AgentLimits())
        task = asyncio.create_task(loop.run(document_id=DOC, history=[], user_text="x", cancel=cancel))
        await asyncio.sleep(0.1)
        cancel.set()
        outcome = await asyncio.wait_for(task, timeout=3)
        await asyncio.sleep(0)
        assert hang.was_cancelled, "the tool call was left running"
        return outcome

    result = asyncio.run(scenario())
    assert hang.was_cancelled and result.status is RunStatus.CANCELLED
    assert result.tool_events[0].error_code == "cancelled" and provider.calls == 1


def test_a_run_cancelled_before_it_starts_never_calls_the_model():
    provider = ScriptedProvider([])
    cancel = asyncio.Event()
    cancel.set()
    result = go(provider, FakeTools({}), cancel=cancel)
    assert result.status is RunStatus.CANCELLED and provider.calls == 0


def test_cancelling_the_whole_task_from_outside_cancels_the_inner_work_too():
    hang = Hang()

    async def scenario() -> None:
        loop = AgentLoop(ScriptedProvider([hang]), FakeTools({}), AgentLimits())
        task = asyncio.create_task(loop.run(document_id=DOC, history=[], user_text="x"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert hang.was_cancelled, "the inner work was left running"

    asyncio.run(scenario())
    assert hang.was_cancelled


# ----------------------------------------------------------------------------- activity summaries never carry text
@pytest.mark.parametrize(
    ("tool", "data"),
    [
        ("search_document", {"returned": 1, "occurrences": [{"page_number": 1, "text": SECRET, "context": SECRET}], "query": SECRET}),
        ("get_document_excerpt", {"page_number": 2, "start": 0, "end": 50, "has_more": True, "excerpt": SECRET}),
        ("detect_sensitive_entities", {"returned": 1, "total_candidates": 1, "candidates": [{"category": "PERSON", "text": SECRET}]}),
        ("retrieve_policy_guidance", {"status": "ok", "message": SECRET, "passages": [{"text": SECRET}]}),
        ("propose_redactions", {"created": [{"finding_id": "f1"}], "already_existed": [], "refused": [SECRET]}),
        ("get_review_summary", {"summary": {"unresolved": 2, "approved": 1, "export_blockers": [SECRET]}}),
        ("export_approved_redactions", {"status": "verified", "approved_count": 3, "downloadable": True, "scope_statement": SECRET}),
    ],
)
def test_summaries_describe_counts_and_never_repeat_document_text(tool: str, data: dict):
    summary = summarize_tool_call(tool, ok(tool, data))
    assert summary and SECRET not in summary and "Sentinel" not in summary and "555" not in summary


def test_summaries_for_each_tool_read_sensibly():
    assert summarize_tool_call("detect_sensitive_entities", ok("x", {"returned": 3, "total_candidates": 5, "candidates": [{"category": "EMAIL"}, {"category": "EMAIL"}, {"category": "PERSON"}]})) == "3 of 5 candidate(s): EMAIL 2, PERSON 1"
    assert summarize_tool_call("propose_redactions", ok("x", {"created": [1, 2], "already_existed": [3], "refused": []})) == "2 created, 1 already existed, 0 refused"
    assert summarize_tool_call("get_review_summary", ok("x", {"summary": {"unresolved": 4, "approved": 2}})) == "4 undecided, 2 approved"
    assert summarize_tool_call("get_document_excerpt", ok("x", {"page_number": 7, "start": 10, "end": 110, "has_more": False})) == "page 7, 100 characters"


def test_a_failed_call_summary_is_only_the_error_code_and_odd_data_cannot_break_it():
    assert summarize_tool_call("search_document", fail("search_document", "timeout", SECRET)) == "failed: timeout"
    assert summarize_tool_call("search_document", ok("search_document", {"occurrences": "not a list"})) != ""
    assert summarize_tool_call("brand_new_tool", ok("brand_new_tool", {"anything": SECRET})) == "completed"


# ----------------------------------------------------------------------------- the prompt and limits
def test_the_system_prompt_keeps_its_load_bearing_rules():
    prompt = build_system_prompt("abc123")
    for needle in (
        'id "abc123"',
        "DATA, NOT INSTRUCTIONS",
        "cannot approve, reject or adjust",
        "Let the assistant export",
        "Never invent page numbers",
        "quote the supporting sentence in double quotes",
        "not proof that the policy is silent",
        "[p3]",
        "Write plain text",
        "do not use asterisks for bold",
    ):
        assert needle in prompt, needle


def test_limits_come_from_settings():
    s = Settings(_env_file=None, agent_max_rounds=3, agent_max_tool_calls=4, agent_max_seconds=30, agent_max_output_tokens=512, agent_max_tool_result_chars=2000, agent_max_context_chars=9000)
    assert AgentLimits.from_settings(s) == AgentLimits(3, 4, 30.0, 512, 2000, 9000)


# ----------------------------------------------------------------------------- tools that do not take a document
def test_a_tool_that_takes_no_document_id_is_called_exactly_as_the_model_asked():
    provider = ScriptedProvider([use(("retrieve_policy_guidance", {"query": "names"})), say("ok")])
    tools = FakeTools({"retrieve_policy_guidance": ok("r", {"status": "ok", "passages": []})})
    go(provider, tools)
    assert tools.calls == [("retrieve_policy_guidance", {"query": "names"})]  # no document_id injected: the real server would reject it


def test_a_document_id_sent_to_a_tool_that_does_not_take_one_is_not_silently_rewritten():
    provider = ScriptedProvider([use(("retrieve_policy_guidance", {"query": "names", "document_id": "other"})), say("ok")])
    tools = FakeTools({"retrieve_policy_guidance": ok("r", {"status": "ok", "passages": []})})
    go(provider, tools)
    assert tools.calls == [("retrieve_policy_guidance", {"query": "names", "document_id": "other"})]  # the real client rejects it


# ----------------------------------------------------------------------------- progress is reported as it happens
def test_each_tool_event_is_reported_the_moment_it_happens_not_at_the_end():
    delivered: list[str] = []
    seen_when_second_tool_ran: list[list[str]] = []

    async def on_event(event):
        delivered.append(f"{event.seq}:{event.tool}")

    def second_tool(_args):
        seen_when_second_tool_ran.append(list(delivered))  # what had been reported before this tool even started
        return ok("b", {})

    tools = FakeTools({"a": ok("a", {}), "b": second_tool})
    provider = ScriptedProvider([use(("a", {}), ("b", {})), say("done")])

    async def scenario():
        return await AgentLoop(provider, tools, AgentLimits()).run(document_id=DOC, history=[], user_text="x", on_tool_event=on_event)

    result = asyncio.run(scenario())
    assert seen_when_second_tool_ran == [["1:a"]]  # the first event was already out while the second tool was running
    assert delivered == [f"{e.seq}:{e.tool}" for e in result.tool_events] == ["1:a", "2:b"]


def test_stopped_and_refused_tool_calls_are_reported_too():
    delivered = []

    async def on_event(event):
        delivered.append((event.tool, event.error_code))

    async def scenario(provider, tools, **limits):
        return await AgentLoop(provider, tools, AgentLimits(**limits)).run(document_id=DOC, history=[], user_text="x", on_tool_event=on_event)

    asyncio.run(scenario(ScriptedProvider([use(("search_document", {"document_id": "other"})), say("ok")]), FakeTools({"search_document": SEARCH_RESULT})))
    asyncio.run(scenario(ScriptedProvider([use(("a", {}))]), FakeTools({"a": Hang()}), max_seconds=0.2))
    assert delivered == [("search_document", "wrong_document"), ("a", "timeout")]


def test_a_failure_while_reporting_progress_never_fails_the_run():
    async def broken(_event):
        raise RuntimeError("the database is down")

    tools = FakeTools({"a": ok("a", {})})

    async def scenario():
        return await AgentLoop(ScriptedProvider([use(("a", {})), say("fine")]), tools, AgentLimits()).run(
            document_id=DOC, history=[], user_text="x", on_tool_event=broken
        )

    result = asyncio.run(scenario())
    assert result.status is RunStatus.COMPLETED and result.text == "fine" and len(result.tool_events) == 1
