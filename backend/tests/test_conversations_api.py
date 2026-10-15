"""Phase 5 step 4: the conversation API and run manager, with a scripted model and fake tool servers.

Nothing here needs a key or the network. The REAL tool server is covered by test_agent_loop_mcp.py.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.agent.loop import AgentLimits, AgentLoop
from app.agent.provider import USER_MESSAGES, ProviderError
from app.config import Settings
from app.db import AGENT_RUNS, CONVERSATIONS, DOCUMENTS, MESSAGES, Mongo
from app.main import create_app
from app.repositories.conversations import MessageRepository, RunRepository
from tests.conftest import make_text_pdf, upload
from tests.support.fake_provider import FakeTools, Hang, ScriptedProvider, ok, say, use

SEARCH = ok("search_document", {"returned": 1, "occurrences": [{"page_number": 1, "text": "hello"}], "unsupported_pages": []})


class FakeToolContext:
    """A ``ToolContextFactory``: every call makes a fake tool server and counts opens and closes."""

    def __init__(self, results: dict | None = None, *, fail_on_open: bool = False) -> None:
        self.results = results if results is not None else {"search_document": SEARCH}
        self.fail_on_open = fail_on_open
        self.opened = 0
        self.closed = 0
        self.runners: list[FakeTools] = []

    def __call__(self, settings):
        outer = self

        class Context:
            async def __aenter__(self):
                outer.opened += 1
                if outer.fail_on_open:
                    raise RuntimeError("cannot start the tool server")
                runner = FakeTools(outer.results)
                outer.runners.append(runner)
                return runner

            async def __aexit__(self, *exc):
                outer.closed += 1

        return Context()


REAL_TOOLS = object()  # pass as `tools=` to use the real MCP server instead of a fake one


def loop_factory(provider: ScriptedProvider, limits: AgentLimits | None = None):
    return lambda settings, tools: AgentLoop(provider, tools, limits or AgentLimits())


@pytest.fixture
def make_client(test_settings: Settings, mongo_client: MongoClient):
    stack = ExitStack()

    def build(*, provider=None, tools: FakeToolContext | None = None, settings: Settings | None = None, drop: bool = True, **state) -> TestClient:
        s = settings or test_settings
        if drop:
            mongo_client.drop_database(s.mongodb_db)
        app = create_app(s)
        if provider is not None:
            app.state.assistant_factory = loop_factory(provider)
        if tools is not REAL_TOOLS:
            app.state.tool_factory = tools or FakeToolContext()
        for name, value in state.items():
            setattr(app.state, name, value)
        return stack.enter_context(TestClient(app))

    yield build
    stack.close()


def new_document(client: TestClient) -> str:
    return upload(client, make_text_pdf([["hello world"]]), "doc.pdf").json()["id"]


def new_conversation(client: TestClient, doc_id: str) -> str:
    r = client.post(f"/api/documents/{doc_id}/conversations", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def send(client: TestClient, cid: str, text: str = "find hello", **extra):
    return client.post(f"/api/conversations/{cid}/messages", json={"content": text, **extra})


def wait_for(client: TestClient, run_id: str, *, until=("completed", "failed", "cancelled"), timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["run"]["status"] in until:
            return detail
        assert time.monotonic() < deadline, f"run stayed {detail['run']['status']}"
        time.sleep(0.05)


# ----------------------------------------------------------------------------- the normal path
def test_create_and_list_conversations_and_start_with_empty_history(make_client):
    client = make_client(provider=ScriptedProvider([]))
    doc = new_document(client)
    assert client.get(f"/api/documents/{doc}/conversations").json() == []
    created = client.post(f"/api/documents/{doc}/conversations", json={"title": "Names"}).json()
    assert created["title"] == "Names" and created["mode"] == "demo" and created["message_count"] == 0
    assert [c["id"] for c in client.get(f"/api/documents/{doc}/conversations").json()] == [created["id"]]
    page = client.get(f"/api/conversations/{created['id']}/messages").json()
    assert page == {"messages": [], "has_more": False}


def test_a_message_starts_a_run_and_the_answer_and_activity_are_stored(make_client):
    provider = ScriptedProvider([use(("search_document", {"query": "hello"})), say("Found it on page 1.")])
    client = make_client(provider=provider)
    cid = new_conversation(client, new_document(client))

    r = send(client, cid)
    assert r.status_code == 202
    body = r.json()
    assert body["duplicate"] is False and body["message"]["seq"] == 1 and body["message"]["role"] == "user"
    assert body["message"]["content"] == "find hello" and body["run"]["status"] in ("queued", "running")

    detail = wait_for(client, body["run"]["id"])
    run = detail["run"]
    assert run["status"] == "completed" and run["rounds"] == 2 and run["tool_calls"] == 1
    assert (run["input_tokens"], run["output_tokens"]) == (20, 10) and run["finished_at"] and run["started_at"]
    assert detail["assistant_message"]["content"] == "Found it on page 1." and detail["assistant_message"]["seq"] == 2
    assert [(e["tool"], e["status"]) for e in detail["tool_events"]] == [("search_document", "ok")]
    assert detail["tool_events"][0]["summary"] == "1 match(es) on page(s) 1; 0 page(s) not searched"

    history = client.get(f"/api/conversations/{cid}/messages").json()["messages"]
    assert [(m["seq"], m["role"], m["status"]) for m in history] == [(1, "user", "complete"), (2, "assistant", "complete")]
    assert client.get(f"/api/documents/{client.get('/api/documents').json()['items'][0]['id']}/conversations").json()[0]["message_count"] == 2


def test_the_conversation_is_still_there_after_the_app_restarts(make_client):
    client = make_client(provider=ScriptedProvider([say("Hello back.")]))
    doc = new_document(client)
    cid = new_conversation(client, doc)
    wait_for(client, send(client, cid).json()["run"]["id"])

    restarted = make_client(provider=ScriptedProvider([]), drop=False)  # a new process on the same database
    messages = restarted.get(f"/api/conversations/{cid}/messages").json()["messages"]
    assert [m["content"] for m in messages] == ["find hello", "Hello back."]
    assert [c["id"] for c in restarted.get(f"/api/documents/{doc}/conversations").json()] == [cid]


def test_history_is_paged_newest_first_and_the_model_sees_only_a_bounded_text_history(make_client, test_settings):
    provider = ScriptedProvider([say(f"answer {i}") for i in range(1, 4)])
    client = make_client(provider=provider, settings=test_settings.model_copy(update={"agent_history_messages": 3}))
    cid = new_conversation(client, new_document(client))
    for i in range(1, 4):
        wait_for(client, send(client, cid, f"question {i}").json()["run"]["id"])

    first_seen = [e for e in provider.transcripts[0]]
    assert len(first_seen) == 1  # nothing before the first question
    third_seen = provider.transcripts[2]
    texts = [getattr(e, "text", None) or e.turn.text for e in third_seen]
    assert texts == ["answer 1", "question 2", "answer 2", "question 3"]  # last 3 stored messages + the new question

    page = client.get(f"/api/conversations/{cid}/messages", params={"limit": 4}).json()
    assert [m["content"] for m in page["messages"]] == ["question 2", "answer 2", "question 3", "answer 3"] and page["has_more"]
    older = client.get(f"/api/conversations/{cid}/messages", params={"limit": 4, "before_seq": page["messages"][0]["seq"]}).json()
    assert [m["content"] for m in older["messages"]] == ["question 1", "answer 1"] and not older["has_more"]


# ----------------------------------------------------------------------------- one run at a time, cancel
def test_a_second_message_while_one_is_running_is_refused_and_cancel_frees_the_conversation(make_client):
    hang = Hang()
    provider = ScriptedProvider([hang, say("ok now")])
    client = make_client(provider=provider)
    cid = new_conversation(client, new_document(client))

    first = send(client, cid, "first").json()["run"]["id"]
    wait_for(client, first, until=("running",))
    busy = send(client, cid, "second")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "run_active"

    cancelled = client.post(f"/api/runs/{first}/cancel")
    assert cancelled.status_code == 202
    detail = wait_for(client, first)
    assert detail["run"]["status"] == "cancelled" and detail["assistant_message"]["status"] == "cancelled"
    assert hang.was_cancelled
    assert client.post(f"/api/runs/{first}/cancel").status_code == 202  # cancelling a finished run is harmless

    again = send(client, cid, "second")
    assert again.status_code == 202
    assert wait_for(client, again.json()["run"]["id"])["assistant_message"]["content"] == "ok now"


def test_a_run_that_is_still_going_when_the_server_stops_is_ended_not_left_active(make_client, test_settings, mongo_client):
    hang = Hang()
    stack_client = make_client(provider=ScriptedProvider([hang]))
    cid = new_conversation(stack_client, new_document(stack_client))
    run_id = send(stack_client, cid).json()["run"]["id"]
    wait_for(stack_client, run_id, until=("running",))
    started = time.monotonic()
    stack_client.__exit__(None, None, None)  # the server stops while the assistant is mid-run
    assert time.monotonic() - started < 5, "shutdown waited for the run's grace period instead of stopping it at once"

    mongo = Mongo(test_settings)
    try:
        run = RunRepository(mongo.db).get("demo-user", run_id)
        assert run.status.value == "cancelled" and run.finished_at is not None
        assert RunRepository(mongo.db).active_for_conversation("demo-user", cid) is None
        assert "shut down" in MessageRepository(mongo.db).list_page("demo-user", cid).messages[-1].content
    finally:
        mongo.close()


def test_the_concurrency_cap_queues_a_second_run_until_a_slot_frees(make_client, test_settings):
    hang = Hang()
    provider = ScriptedProvider([hang, say("second done")])
    client = make_client(provider=provider, settings=test_settings.model_copy(update={"agent_max_concurrent_runs": 1}))
    doc = new_document(client)
    a, b = new_conversation(client, doc), new_conversation(client, doc)

    run_a = send(client, a).json()["run"]["id"]
    wait_for(client, run_a, until=("running",))
    run_b = send(client, b).json()["run"]["id"]
    time.sleep(0.3)
    assert client.get(f"/api/runs/{run_b}").json()["run"]["status"] == "queued"  # waiting for the only slot

    client.post(f"/api/runs/{run_a}/cancel")
    assert wait_for(client, run_b)["assistant_message"]["content"] == "second done"


# ----------------------------------------------------------------------------- idempotent sends
def test_repeating_a_client_message_id_returns_the_original_and_starts_nothing(make_client, mongo_client, test_settings):
    provider = ScriptedProvider([say("only once")])
    client = make_client(provider=provider)
    cid = new_conversation(client, new_document(client))

    first = send(client, cid, client_message_id="retry-me-0001").json()
    wait_for(client, first["run"]["id"])
    again = send(client, cid, client_message_id="retry-me-0001")
    assert again.status_code == 202
    body = again.json()
    assert body["duplicate"] is True and body["message"]["id"] == first["message"]["id"] and body["run"]["id"] == first["run"]["id"]
    assert provider.calls == 1
    db = mongo_client[test_settings.mongodb_db]
    assert db[MESSAGES].count_documents({"conversation_id": cid}) == 2 and db[AGENT_RUNS].count_documents({"conversation_id": cid}) == 1


# ----------------------------------------------------------------------------- ownership and validation
def test_another_owner_cannot_read_write_or_cancel_anything(make_client, test_settings):
    client = make_client(provider=ScriptedProvider([say("private answer")]))
    doc = new_document(client)
    cid = new_conversation(client, doc)
    run_id = send(client, cid).json()["run"]["id"]
    wait_for(client, run_id)

    other = make_client(provider=ScriptedProvider([]), settings=test_settings.model_copy(update={"demo_owner_id": "someone-else"}), drop=False)
    assert other.get(f"/api/documents/{doc}/conversations").status_code == 404
    assert other.post(f"/api/documents/{doc}/conversations", json={}).status_code == 404
    assert other.get(f"/api/conversations/{cid}/messages").status_code == 404
    assert send(other, cid).status_code == 404
    assert other.get(f"/api/runs/{run_id}").status_code == 404
    assert other.post(f"/api/runs/{run_id}/cancel").status_code == 404
    assert "private answer" not in json.dumps(other.get(f"/api/runs/{run_id}").json())


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"content": ""}, 422, "invalid_request"),
        ({"content": "   \n "}, 422, "empty_message"),
        ({"content": "x" * 4001}, 422, "invalid_request"),
        ({"content": "bad \x00 byte"}, 422, "invalid_message"),
        ({"content": "ok", "client_message_id": "short"}, 422, "invalid_request"),
        ({"content": "ok", "client_message_id": "has spaces in it!"}, 422, "invalid_request"),
    ],
)
def test_bad_messages_are_rejected_with_a_stable_code_and_store_nothing(make_client, mongo_client, test_settings, payload, status, code):
    client = make_client(provider=ScriptedProvider([]))
    cid = new_conversation(client, new_document(client))
    r = client.post(f"/api/conversations/{cid}/messages", json=payload)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert mongo_client[test_settings.mongodb_db][MESSAGES].count_documents({}) == 0


def test_unknown_ids_are_404_and_a_document_that_is_not_ready_is_409(make_client, mongo_client, test_settings):
    client = make_client(provider=ScriptedProvider([]))
    missing = "000000000000000000000000"
    assert send(client, missing).status_code == 404
    assert client.get(f"/api/conversations/{missing}/messages").status_code == 404
    assert client.get(f"/api/runs/{missing}").status_code == 404
    doc = new_document(client)
    cid = new_conversation(client, doc)
    from bson import ObjectId

    mongo_client[test_settings.mongodb_db][DOCUMENTS].update_one({"_id": ObjectId(doc)}, {"$set": {"status": "error"}})
    r = send(client, cid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "document_not_ready"


# ----------------------------------------------------------------------------- failures are visible and recoverable
def test_a_provider_failure_ends_the_run_with_a_fixed_message_and_the_conversation_keeps_working(make_client):
    provider = ScriptedProvider([ProviderError("rate_limited", "RAW BODY", retry_after_seconds=30, request_id="req_7"), say("fine now")])
    client = make_client(provider=provider)
    cid = new_conversation(client, new_document(client))

    failed = wait_for(client, send(client, cid, "first").json()["run"]["id"])
    assert failed["run"]["status"] == "failed" and failed["run"]["error_code"] == "rate_limited"
    assert failed["run"]["request_ids"] == ["req_7"] and "RAW BODY" not in json.dumps(failed)
    assert failed["assistant_message"]["status"] == "failed" and failed["assistant_message"]["content"].startswith(USER_MESSAGES["rate_limited"])

    retry = wait_for(client, send(client, cid, "first again").json()["run"]["id"])
    assert retry["run"]["status"] == "completed" and retry["assistant_message"]["content"] == "fine now"
    # the failure notice is not shown to the model as if it were part of the conversation
    second_transcript = provider.transcripts[1]
    assert [getattr(e, "text", None) for e in second_transcript if hasattr(e, "text")] == ["first", "first again"]


def test_a_tool_server_that_cannot_start_fails_the_run_cleanly(make_client):
    client = make_client(provider=ScriptedProvider([]), tools=FakeToolContext(fail_on_open=True))
    cid = new_conversation(client, new_document(client))
    detail = wait_for(client, send(client, cid).json()["run"]["id"])
    assert detail["run"]["status"] == "failed" and detail["run"]["error_code"] == "tools_unavailable"
    assert detail["assistant_message"]["content"] == USER_MESSAGES["tools_unavailable"] and "cannot start" not in json.dumps(detail)
    assert send(client, cid).status_code in (202, 409)  # not stuck: the next request is accepted (or sees the old run end)


def test_live_mode_without_a_usable_provider_is_refused_up_front_and_stores_nothing(make_client, mongo_client, test_settings):
    live = test_settings.model_copy(update={"assistant_mode": "live", "llm_provider": "anthropic", "llm_api_key": "", "llm_model": "claude-opus-5-5"})
    client = make_client(settings=live)  # no scripted assistant: the real configuration check runs
    cid = new_conversation(client, new_document(client))
    r = send(client, cid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "assistant_not_configured"
    assert "CLEARCASE_LLM_API_KEY" in r.json()["error"]["message"] and "Nothing was sent" in r.json()["error"]["message"]
    db = mongo_client[live.mongodb_db]
    assert db[MESSAGES].count_documents({}) == 0 and db[AGENT_RUNS].count_documents({}) == 0


def test_demo_mode_runs_without_any_provider_or_key(make_client):
    client = make_client()  # default factory, default settings: demo mode
    cid = new_conversation(client, new_document(client))
    detail = wait_for(client, send(client, cid, "help").json()["run"]["id"])
    assert detail["run"]["status"] == "completed" and detail["run"]["mode"] == "demo" and detail["run"]["provider"] is None


# ----------------------------------------------------------------------------- documents and the assistant
def test_a_document_cannot_be_deleted_while_the_assistant_is_working_on_it(make_client):
    hang = Hang()
    client = make_client(provider=ScriptedProvider([hang]))
    doc = new_document(client)
    cid = new_conversation(client, doc)
    run_id = send(client, cid).json()["run"]["id"]
    wait_for(client, run_id, until=("running",))
    refused = client.delete(f"/api/documents/{doc}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "assistant_active"

    client.post(f"/api/runs/{run_id}/cancel")
    wait_for(client, run_id)
    deleted = client.delete(f"/api/documents/{doc}")
    assert deleted.status_code == 200 and deleted.json()["deleted"]["conversations"] == 1


# ----------------------------------------------------------------------------- tool server sessions
def test_one_tool_server_is_reused_across_runs_of_a_conversation_and_closed_when_idle_or_at_shutdown(make_client, test_settings):
    tools = FakeToolContext()
    provider = ScriptedProvider([use(("search_document", {"query": "a"})), say("one"), use(("search_document", {"query": "b"})), say("two"), say("three")])
    client = make_client(provider=provider, tools=tools, settings=test_settings.model_copy(update={"agent_tool_idle_seconds": 0.0}))
    doc = new_document(client)
    a, b = new_conversation(client, doc), new_conversation(client, doc)

    wait_for(client, send(client, a).json()["run"]["id"])
    wait_for(client, send(client, a, "again").json()["run"]["id"])
    assert tools.opened == 1  # the second run of the same conversation reused the server
    wait_for(client, send(client, b).json()["run"]["id"])
    assert tools.opened == 2  # another conversation gets its own

    manager = client.app.state.run_manager
    assert client.portal.call(manager.pool.sweep) == 2  # idle for >= 0 s: both closed
    assert tools.closed == 2 and manager.pool.open_sessions == 0
    wait_for(client, send(client, a, "after idle").json()["run"]["id"])  # transparently reopened
    assert tools.opened == 3
    client.__exit__(None, None, None)  # shutting the app down closes whatever is still open
    assert tools.closed == 3


def test_a_tool_server_that_dies_mid_call_is_restarted_once_and_the_call_retried(make_client):
    from tests.support.fake_provider import fail

    state = {"calls": 0}

    def flaky(_args):
        state["calls"] += 1
        return fail("search_document", "transport_error", "pipe closed") if state["calls"] == 1 else SEARCH

    tools = FakeToolContext({"search_document": flaky})
    client = make_client(provider=ScriptedProvider([use(("search_document", {"query": "a"})), say("recovered")]), tools=tools)
    cid = new_conversation(client, new_document(client))
    detail = wait_for(client, send(client, cid).json()["run"]["id"])
    assert detail["run"]["status"] == "completed" and detail["assistant_message"]["content"] == "recovered"
    assert tools.opened == 2 and tools.closed >= 1 and state["calls"] == 2  # restarted once, retried once
    assert detail["tool_events"][0]["status"] == "ok"


# ----------------------------------------------------------------------------- health
def test_health_describes_the_assistant_without_exposing_the_key(make_client, test_settings):
    demo = make_client(provider=ScriptedProvider([])).get("/api/health").json()
    assert demo["assistant"] == {"mode": "demo", "provider": None, "model": None, "configured": True, "sends_excerpts_to_provider": False}

    secret = "sk-ant-api03-SUPERSECRET"
    live = test_settings.model_copy(update={"assistant_mode": "live", "llm_provider": "anthropic", "llm_api_key": secret, "llm_model": "claude-opus-5-5"})
    live_body = make_client(settings=live).get("/api/health")
    assert live_body.json()["assistant"] == {"mode": "live", "provider": "anthropic", "model": "claude-opus-5-5", "configured": True, "sends_excerpts_to_provider": True}
    assert secret not in live_body.text

    unconfigured = live.model_copy(update={"llm_api_key": ""})
    assert make_client(settings=unconfigured).get("/api/health").json()["assistant"]["configured"] is False


def test_the_conversation_rows_exist_as_documented(make_client, mongo_client, test_settings):
    client = make_client(provider=ScriptedProvider([say("hi")]))
    doc = new_document(client)
    cid = new_conversation(client, doc)
    wait_for(client, send(client, cid).json()["run"]["id"])
    stored = mongo_client[test_settings.mongodb_db][CONVERSATIONS].find_one({})
    assert stored["document_id"] == doc and stored["owner_id"] == "demo-user" and stored["mode"] == "demo"


def test_the_real_tool_server_is_opened_reused_closed_and_reopened_without_transport_errors(make_client, test_settings, sample_contract_bytes, caplog):
    """The MCP transport must be closed by the same task that opened it; fakes cannot prove that, the real server can."""
    import logging

    provider = ScriptedProvider(
        [
            use(("search_document", {"query": "Maria"})), say("first"),
            use(("search_document", {"query": "Jose"})), say("second"),
            use(("search_document", {"query": "Priya"})), say("third"),
        ]
    )
    client = make_client(provider=provider, tools=REAL_TOOLS, settings=test_settings.model_copy(update={"agent_tool_idle_seconds": 0.0}))
    doc = upload(client, sample_contract_bytes, "sample.pdf").json()["id"]
    cid = new_conversation(client, doc)
    manager = client.app.state.run_manager

    with caplog.at_level(logging.WARNING):
        first = wait_for(client, send(client, cid, "one").json()["run"]["id"], timeout=60)
        second = wait_for(client, send(client, cid, "two").json()["run"]["id"], timeout=60)
        assert first["run"]["status"] == second["run"]["status"] == "completed"
        assert [e["status"] for e in first["tool_events"] + second["tool_events"]] == ["ok", "ok"]
        assert manager.pool.open_sessions == 1  # one server process served both runs

        assert client.portal.call(manager.pool.sweep) == 1  # idle: closed by its own task, cleanly
        assert manager.pool.open_sessions == 0
        third = wait_for(client, send(client, cid, "three").json()["run"]["id"], timeout=60)  # reopened on demand
        assert third["run"]["status"] == "completed"
        client.__exit__(None, None, None)  # shutdown closes the reopened server too

    problems = [r.getMessage() for r in caplog.records if "Tool session ended" in r.getMessage() or "cancel scope" in r.getMessage()]
    assert problems == []


# ----------------------------------------------------------------------------- citations are checked, not trusted
POLICY_ID = "northwind-redaction-policy:1.0:3"
POLICY_RESULT = ok(
    "retrieve_policy_guidance",
    {"status": "ok", "passages": [{"citation_id": POLICY_ID, "text": "Names must be redacted before sharing."}]},
)


def test_citations_in_the_answer_are_verified_against_what_the_tools_returned_and_stored_with_the_message(make_client):
    answer = (
        'Page [p1] has the name; [p7] was never in my results. The policy says "Names must be redacted before sharing." '
        f'[{POLICY_ID}] and also "Names may be shared freely." [{POLICY_ID}].'
    )
    provider = ScriptedProvider(
        [use(("search_document", {"query": "x"}), ("retrieve_policy_guidance", {"query": "names"})), say(answer)]
    )
    client = make_client(provider=provider, tools=FakeToolContext({"search_document": SEARCH, "retrieve_policy_guidance": POLICY_RESULT}))
    cid = new_conversation(client, new_document(client))
    detail = wait_for(client, send(client, cid).json()["run"]["id"])

    citations = detail["assistant_message"]["citations"]
    assert [(c["kind"], c["ref"], c["verified"]) for c in citations] == [
        ("page", "1", True),
        ("page", "7", False),
        ("policy", POLICY_ID, True),
        ("policy", POLICY_ID, False),
    ]
    assert citations[2]["quote"] == "Names must be redacted before sharing." and citations[3]["quote"] == "Names may be shared freely."
    assert detail["run"]["unverified_citations"] == 2
    # and they come back with the history after a reload
    stored = client.get(f"/api/conversations/{cid}/messages").json()["messages"][-1]
    assert stored["citations"] == citations


def test_a_failed_run_stores_no_citations(make_client):
    client = make_client(provider=ScriptedProvider([ProviderError("provider_unavailable")]))
    cid = new_conversation(client, new_document(client))
    detail = wait_for(client, send(client, cid).json()["run"]["id"])
    assert detail["assistant_message"]["citations"] == [] and detail["run"]["unverified_citations"] == 0


# ----------------------------------------------------------------------------- progress while a request is running
def test_the_activity_log_fills_in_while_the_request_runs_and_is_not_duplicated_at_the_end(make_client, mongo_client, test_settings):
    hang = Hang()
    provider = ScriptedProvider([use(("search_document", {"query": "hello"})), hang])
    client = make_client(provider=provider)
    cid = new_conversation(client, new_document(client))
    run_id = send(client, cid).json()["run"]["id"]

    deadline = time.monotonic() + 10
    while True:  # the first tool has finished; the model is now "thinking" (hanging) and the request is still running
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["tool_events"]:
            break
        assert time.monotonic() < deadline, "no progress was reported while the request was running"
        time.sleep(0.05)
    assert detail["run"]["status"] == "running" and detail["assistant_message"] is None
    assert [(e["seq"], e["tool"], e["status"]) for e in detail["tool_events"]] == [(1, "search_document", "ok")]

    client.post(f"/api/runs/{run_id}/cancel")
    final = wait_for(client, run_id)
    assert [(e["seq"], e["tool"]) for e in final["tool_events"]] == [(1, "search_document")]  # still exactly one
    assert mongo_client[test_settings.mongodb_db]["tool_events"].count_documents({"run_id": run_id}) == 1
