"""Phase 5 step 1: conversation storage, ordering, ownership, idempotency, cascade and restart recovery."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.db import AGENT_RUNS, CONVERSATIONS, MESSAGES, TOOL_EVENTS, Mongo
from app.main import create_app
from app.models.conversations import (
    MAX_ERROR_CHARS,
    MAX_SUMMARY_CHARS,
    AssistantMode,
    Citation,
    CitationKind,
    MessageRole,
    MessageStatus,
    RunStatus,
    ToolEventStatus,
)
from app.repositories.conversations import (
    ConversationRepository,
    MessageRepository,
    RunRepository,
    ToolEventRepository,
)
from app.services.conversation_service import INTERRUPTED_NOTICE, recover_interrupted_runs
from app.services.errors import ConflictError, NotFoundError
from tests.conftest import make_text_pdf, upload

OWNER, OTHER = "demo-user", "someone-else"


@pytest.fixture
def mongo(test_settings: Settings, mongo_client: MongoClient):
    mongo_client.drop_database(test_settings.mongodb_db)
    m = Mongo(test_settings)
    m.ensure_indexes()
    yield m
    m.close()


@pytest.fixture
def repos(mongo: Mongo):
    db = mongo.db
    return ConversationRepository(db), MessageRepository(db), RunRepository(db), ToolEventRepository(db)


def new_conversation(conversations: ConversationRepository, document_id: str = "doc1", owner: str = OWNER):
    return conversations.create(owner_id=owner, document_id=document_id, mode=AssistantMode.DEMO, title="Chat")


def say(messages: MessageRepository, conversation_id: str, text: str, *, role=MessageRole.USER, owner: str = OWNER, **kw):
    return messages.append(owner_id=owner, conversation_id=conversation_id, role=role, content=text, **kw)


# ----------------------------------------------------------------------------- ordering and paging
def test_messages_get_increasing_sequence_numbers_and_are_counted(repos):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    stored = [say(messages, c.id, f"m{i}")[0] for i in range(1, 6)]
    assert [m.seq for m in stored] == [1, 2, 3, 4, 5]
    refreshed = conversations.get(OWNER, c.id)
    assert refreshed.message_count == 5 and refreshed.version == 6  # created at 1, +1 per message


def test_history_pages_backwards_with_no_gaps_or_repeats(repos):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    for i in range(1, 8):
        say(messages, c.id, f"m{i}")
    newest = messages.list_page(OWNER, c.id, limit=3)
    assert [m.content for m in newest.messages] == ["m5", "m6", "m7"] and newest.has_more
    older = messages.list_page(OWNER, c.id, before_seq=newest.messages[0].seq, limit=3)
    assert [m.content for m in older.messages] == ["m2", "m3", "m4"] and older.has_more
    oldest = messages.list_page(OWNER, c.id, before_seq=older.messages[0].seq, limit=3)
    assert [m.content for m in oldest.messages] == ["m1"] and not oldest.has_more


def test_concurrent_appends_never_share_a_sequence_number(repos):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            say(messages, c.id, f"t{n}")
        except Exception as exc:  # noqa: BLE001 - the test reports any failure below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    seqs = [m.seq for m in messages.list_page(OWNER, c.id, limit=100).messages]
    assert len(seqs) == 20 and len(set(seqs)) == 20 and seqs == sorted(seqs)


def test_model_history_is_recent_text_only_and_leaves_out_app_error_notices(repos):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    say(messages, c.id, "old question")
    say(messages, c.id, "old answer", role=MessageRole.ASSISTANT)
    say(messages, c.id, "interrupted request")
    say(messages, c.id, INTERRUPTED_NOTICE, role=MessageRole.ASSISTANT, status=MessageStatus.FAILED)
    say(messages, c.id, "new question")
    history = messages.recent_for_model(OWNER, c.id, limit=3)
    assert history == [("assistant", "old answer"), ("user", "interrupted request"), ("user", "new question")]


def test_citations_round_trip(repos):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    cites = [
        Citation(kind=CitationKind.PAGE, ref="3", verified=True),
        Citation(kind=CitationKind.POLICY, ref="p:1.0:3", verified=False, quote="Names must be redacted."),
    ]
    stored, _ = say(messages, c.id, "answer", role=MessageRole.ASSISTANT, citations=cites)
    assert messages.get(OWNER, stored.id).citations == cites


# ----------------------------------------------------------------------------- idempotent sends
def test_a_repeated_client_message_id_stores_one_message(repos, mongo):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    first, created_first = say(messages, c.id, "find emails", client_message_id="abc")
    again, created_again = say(messages, c.id, "find emails", client_message_id="abc")
    assert created_first and not created_again and again.id == first.id
    assert mongo.db[MESSAGES].count_documents({"conversation_id": c.id}) == 1
    # the same id in a different conversation is a different message
    other = new_conversation(conversations)
    assert say(messages, other.id, "find emails", client_message_id="abc")[1] is True


def test_the_database_itself_refuses_a_duplicate_client_message_id(repos, mongo):
    # Bypass the repository: the unique index must hold even if application code has a bug or a race.
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    say(messages, c.id, "x", client_message_id="dup")
    stored = mongo.db[MESSAGES].find_one({"conversation_id": c.id})
    clone = {k: v for k, v in stored.items() if k != "_id"} | {"seq": 99}
    with pytest.raises(DuplicateKeyError):
        mongo.db[MESSAGES].insert_one(clone)


def test_messages_without_a_client_id_do_not_collide(repos):
    conversations, messages, _, _ = repos
    c = new_conversation(conversations)
    say(messages, c.id, "a")
    say(messages, c.id, "b")  # both have client_message_id None: the partial index must ignore them


# ----------------------------------------------------------------------------- ownership
def test_another_owner_cannot_see_or_write_anything(repos):
    conversations, messages, runs, events = repos
    c = new_conversation(conversations)
    stored, _ = say(messages, c.id, "private")
    run = runs.create(owner_id=OWNER, conversation_id=c.id, document_id="doc1", mode=AssistantMode.DEMO)
    events.record(owner_id=OWNER, run_id=run.id, document_id="doc1", seq=1, tool="t", status=ToolEventStatus.OK)

    with pytest.raises(NotFoundError):
        conversations.get(OTHER, c.id)
    with pytest.raises(NotFoundError):
        messages.list_page(OTHER, c.id)
    with pytest.raises(NotFoundError):
        say(messages, c.id, "intruder", owner=OTHER)
    with pytest.raises(NotFoundError):
        messages.get(OTHER, stored.id)
    with pytest.raises(NotFoundError):
        runs.get(OTHER, run.id)
    assert runs.active_for_conversation(OTHER, c.id) is None
    assert events.list_for_run(OTHER, run.id) == []
    assert conversations.list_for_document(OTHER, "doc1") == []
    assert messages.recent_for_model(OTHER, c.id, limit=10) == []


def test_unknown_or_malformed_ids_are_not_found_not_errors(repos):
    conversations, _, runs, _ = repos
    with pytest.raises(NotFoundError):
        conversations.get(OWNER, "000000000000000000000000")
    with pytest.raises(Exception) as exc:  # a malformed id must not leak a 500-style InvalidId
        runs.get(OWNER, "not-an-object-id")
    assert type(exc.value).__name__ in {"NotFoundError", "ValidationFailed"}


# ----------------------------------------------------------------------------- runs
def test_only_one_run_can_be_active_per_conversation(repos):
    conversations, _, runs, _ = repos
    a, b = new_conversation(conversations), new_conversation(conversations)
    first = runs.create(owner_id=OWNER, conversation_id=a.id, document_id="doc1", mode=AssistantMode.LIVE, provider="anthropic", model="m")
    with pytest.raises(ConflictError) as exc:
        runs.create(owner_id=OWNER, conversation_id=a.id, document_id="doc1", mode=AssistantMode.LIVE)
    assert exc.value.code == "run_active" and exc.value.status_code == 409
    runs.create(owner_id=OWNER, conversation_id=b.id, document_id="doc1", mode=AssistantMode.LIVE)  # other conversation: fine
    assert runs.active_for_conversation(OWNER, a.id).id == first.id

    runs.finish(OWNER, first.id, status=RunStatus.COMPLETED)
    assert runs.active_for_conversation(OWNER, a.id) is None
    runs.create(owner_id=OWNER, conversation_id=a.id, document_id="doc1", mode=AssistantMode.LIVE)  # free again


def test_the_database_itself_allows_only_one_active_run(repos, mongo):
    conversations, _, runs, _ = repos
    c = new_conversation(conversations)
    runs.create(owner_id=OWNER, conversation_id=c.id, document_id="doc1", mode=AssistantMode.DEMO)
    existing = mongo.db[AGENT_RUNS].find_one({"conversation_id": c.id})
    clone = {k: v for k, v in existing.items() if k != "_id"}
    with pytest.raises(DuplicateKeyError):
        mongo.db[AGENT_RUNS].insert_one(clone)
    clone["active"] = False  # finished runs are unlimited
    mongo.db[AGENT_RUNS].insert_one(clone)


def test_finishing_a_run_records_the_outcome_and_truncates_long_errors(repos):
    conversations, _, runs, _ = repos
    c = new_conversation(conversations)
    run = runs.create(owner_id=OWNER, conversation_id=c.id, document_id="doc1", mode=AssistantMode.LIVE, provider="anthropic", model="m")
    assert run.status is RunStatus.QUEUED
    runs.mark_running(OWNER, run.id)
    assert runs.get(OWNER, run.id).started_at is not None
    done = runs.finish(
        OWNER,
        run.id,
        status=RunStatus.FAILED,
        rounds=3,
        tool_calls=5,
        limits_hit=["max_seconds"],
        error_code="rate_limited",
        error="x" * 5000,
        input_tokens=1200,
        output_tokens=300,
        request_ids=["req_1"],
        unverified_citations=2,
        assistant_message_id="m1",
    )
    assert done.status is RunStatus.FAILED and done.finished_at is not None
    assert (done.rounds, done.tool_calls, done.limits_hit, done.input_tokens, done.output_tokens) == (3, 5, ["max_seconds"], 1200, 300)
    assert done.request_ids == ["req_1"] and done.unverified_citations == 2
    assert len(done.error) == MAX_ERROR_CHARS
    with pytest.raises(ValueError):
        runs.finish(OWNER, run.id, status=RunStatus.RUNNING)


def test_tool_events_are_ordered_truncated_and_unique_per_run(repos, mongo):
    conversations, _, runs, events = repos
    c = new_conversation(conversations)
    run = runs.create(owner_id=OWNER, conversation_id=c.id, document_id="doc1", mode=AssistantMode.DEMO)
    for seq, tool in [(2, "propose_redactions"), (1, "search_document")]:
        events.record(
            owner_id=OWNER, run_id=run.id, document_id="doc1", seq=seq, tool=tool,
            status=ToolEventStatus.OK, duration_ms=12, summary="s" * 1000,
        )
    listed = events.list_for_run(OWNER, run.id)
    assert [e.tool for e in listed] == ["search_document", "propose_redactions"]
    assert all(len(e.summary) == MAX_SUMMARY_CHARS for e in listed)
    with pytest.raises(DuplicateKeyError):
        events.record(owner_id=OWNER, run_id=run.id, document_id="doc1", seq=1, tool="x", status=ToolEventStatus.ERROR)


# ----------------------------------------------------------------------------- delete cascade
def test_deleting_a_document_removes_its_conversations_and_everything_under_them(client: TestClient, mongo_client, test_settings):
    keep_id = upload(client, make_text_pdf([["keep"]]), "keep.pdf").json()["id"]
    drop_id = upload(client, make_text_pdf([["drop"]]), "drop.pdf").json()["id"]
    db = mongo_client[test_settings.mongodb_db]
    conversations, messages, runs, events = (
        ConversationRepository(db), MessageRepository(db), RunRepository(db), ToolEventRepository(db),
    )
    ids = {}
    for name, doc_id in (("keep", keep_id), ("drop", drop_id)):
        c = new_conversation(conversations, doc_id)
        say(messages, c.id, f"{name} question")
        say(messages, c.id, f"{name} answer", role=MessageRole.ASSISTANT)
        run = runs.create(owner_id=OWNER, conversation_id=c.id, document_id=doc_id, mode=AssistantMode.DEMO)
        events.record(owner_id=OWNER, run_id=run.id, document_id=doc_id, seq=1, tool="search_document", status=ToolEventStatus.OK)
        runs.finish(OWNER, run.id, status=RunStatus.COMPLETED)  # a document with a run still going cannot be deleted
        ids[name] = c.id

    deleted = client.delete(f"/api/documents/{drop_id}").json()["deleted"]
    assert (deleted["conversations"], deleted["messages"], deleted["agent_runs"], deleted["tool_events"]) == (1, 2, 1, 1)
    assert db[CONVERSATIONS].count_documents({"document_id": drop_id}) == 0
    assert db[MESSAGES].count_documents({"conversation_id": ids["drop"]}) == 0
    assert db[AGENT_RUNS].count_documents({"document_id": drop_id}) == 0
    assert db[TOOL_EVENTS].count_documents({"document_id": drop_id}) == 0
    # the other document's conversation is untouched
    assert db[CONVERSATIONS].count_documents({"document_id": keep_id}) == 1
    assert db[MESSAGES].count_documents({"conversation_id": ids["keep"]}) == 2
    assert db[AGENT_RUNS].count_documents({"document_id": keep_id}) == 1
    assert db[TOOL_EVENTS].count_documents({"document_id": keep_id}) == 1


# ----------------------------------------------------------------------------- restart recovery
def test_runs_left_active_by_a_previous_process_are_failed_with_a_visible_notice(repos, mongo):
    conversations, messages, runs, _ = repos
    stuck_conv, fine_conv = new_conversation(conversations), new_conversation(conversations)
    stuck = runs.create(owner_id=OWNER, conversation_id=stuck_conv.id, document_id="doc1", mode=AssistantMode.LIVE)
    runs.mark_running(OWNER, stuck.id)
    done = runs.create(owner_id=OWNER, conversation_id=fine_conv.id, document_id="doc1", mode=AssistantMode.LIVE)
    runs.finish(OWNER, done.id, status=RunStatus.COMPLETED)
    other_owner_conv = new_conversation(conversations, owner=OTHER)
    other_stuck = runs.create(owner_id=OTHER, conversation_id=other_owner_conv.id, document_id="doc1", mode=AssistantMode.DEMO)

    assert recover_interrupted_runs(mongo) == 2  # every owner's stuck runs, not only the demo user's

    failed = runs.get(OWNER, stuck.id)
    assert failed.status is RunStatus.FAILED and failed.error_code == "interrupted" and failed.finished_at is not None
    assert runs.get(OTHER, other_stuck.id).status is RunStatus.FAILED
    assert runs.get(OWNER, done.id).status is RunStatus.COMPLETED  # untouched
    notice = messages.list_page(OWNER, stuck_conv.id).messages[-1]
    assert notice.role is MessageRole.ASSISTANT and notice.status is MessageStatus.FAILED
    assert notice.content == INTERRUPTED_NOTICE and notice.run_id == stuck.id
    assert messages.list_page(OWNER, fine_conv.id).messages == []  # a healthy conversation gets no notice
    # the conversation can be used again, and recovery is safe to repeat
    runs.create(owner_id=OWNER, conversation_id=stuck_conv.id, document_id="doc1", mode=AssistantMode.LIVE)
    assert recover_interrupted_runs(mongo) == 1  # the run just created counts as stuck for this "new process"
    assert len(messages.list_page(OWNER, stuck_conv.id).messages) == 2


def test_the_app_recovers_interrupted_runs_when_it_starts(test_settings, mongo_client):
    mongo_client.drop_database(test_settings.mongodb_db)
    seed = Mongo(test_settings)
    try:
        seed.ensure_indexes()
        c = new_conversation(ConversationRepository(seed.db))
        runs = RunRepository(seed.db)
        run = runs.create(owner_id=OWNER, conversation_id=c.id, document_id="doc1", mode=AssistantMode.LIVE)
        runs.mark_running(OWNER, run.id)
    finally:
        seed.close()

    with TestClient(create_app(test_settings)):  # starting the app is the "restart"
        pass

    check = Mongo(test_settings)
    try:
        assert RunRepository(check.db).get(OWNER, run.id).status is RunStatus.FAILED
        assert MessageRepository(check.db).list_page(OWNER, c.id).messages[-1].content == INTERRUPTED_NOTICE
    finally:
        check.close()
