"""Demo mode: scripted commands over canned tool results, plus the real tool server end to end.

The first half uses fake tools so every reply can be pinned exactly. The second half drives the real MCP server, the real
database and the run manager, including the hand-off: propose -> reviewer approves -> reviewer authorizes -> export.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.agent.citations import verify_citations
from app.agent.demo import DEFAULT_PROPOSE, HELP, DemoAssistant
from app.agent.loop import AgentLimits, LoopResult
from app.mcp_server.schemas import MAX_PROPOSALS_PER_CALL
from app.models.conversations import RunStatus
from tests.conftest import make_text_pdf, requires_embedding_model, upload
from tests.support.fake_provider import FakeTools, Hang, fail, ok

DOC = "doc1"
PASSAGE_ID = "northwind-redaction-policy:1.0:3"
PASSAGE_TEXT = "Company names remain visible unless a reviewer asks otherwise. Individuals' names must be redacted."


def run(tools: FakeTools, text: str, *, limits: AgentLimits | None = None, cancel=None, after=None) -> LoopResult:
    async def scenario() -> LoopResult:
        result = await DemoAssistant(tools, limits).run(document_id=DOC, history=[], user_text=text, cancel=cancel)
        await asyncio.sleep(0)
        if after:
            after()
        return result

    return asyncio.run(scenario())


def candidate(n: int, category: str = "EMAIL") -> dict:
    return {
        "candidate_id": f"c{n}", "category": category, "page_number": 1, "char_start": n * 10, "char_end": n * 10 + 5,
        "text": f"x{n}@example.com", "sources": ["pattern:email"], "mappable": True,
    }  # fmt: skip


def detection(*candidates: dict, notes=(), truncated=False, total=None) -> dict:
    return {"total_candidates": total or len(candidates), "returned": len(candidates), "truncated": truncated, "candidates": list(candidates), "notes": list(notes)}


# ----------------------------------------------------------------------------- every reply is labelled and deterministic
@pytest.mark.parametrize("text", ["help", "/help", "HELP", "", "   ", "what is the weather", "tell me a joke"])
def test_help_and_unrecognised_input_show_the_commands_and_call_no_tool(text: str):
    tools = FakeTools({})
    result = run(tools, text)
    assert result.status is RunStatus.COMPLETED and tools.calls == [] and result.tool_events == []
    assert HELP in result.text and result.text.endswith("No AI model was used.)")  # literal on purpose: not the module's own constant
    assert ("I did not understand" in result.text) == (text.strip().lower().lstrip("/") not in ("help", ""))


def test_the_same_input_gives_the_same_reply():
    data = {"returned": 1, "truncated": False, "occurrences": [{"page_number": 2, "context": "…Jane Doe…"}], "unsupported_pages": []}
    first = run(FakeTools({"search_document": ok("search_document", data)}), "find Jane").text
    second = run(FakeTools({"search_document": ok("search_document", data)}), "find Jane").text
    assert first == second


# ----------------------------------------------------------------------------- find
def test_find_lists_matches_with_pages_and_notes_pages_that_were_not_searched():
    data = {
        "returned": 2, "truncated": True,
        "occurrences": [{"page_number": 1, "context": "contact Jane\nDoe  at  home"}, {"page_number": 3, "context": "Jane again"}],
        "unsupported_pages": [4],
    }  # fmt: skip
    tools = FakeTools({"search_document": ok("search_document", data)})
    text = run(tools, "find Jane Doe").text
    assert tools.calls == [("search_document", {"query": "Jane Doe", "max_results": 10, "document_id": DOC})]
    assert "Found 2+ match(es)" in text and "page 1: contact Jane Doe at home" in text and "page 3: Jane again" in text
    assert "and more" in text and "Pages 4 are scanned or mixed and were not searched" in text


def test_find_with_no_matches_or_no_query_or_a_tool_error():
    none = {"returned": 0, "truncated": False, "occurrences": [], "unsupported_pages": []}
    assert "No matches for “zzz”" in run(FakeTools({"search_document": ok("search_document", none)}), "find zzz").text
    tools = FakeTools({})
    assert "Tell me what to look for" in run(tools, "find").text and tools.calls == []
    broken = run(FakeTools({"search_document": fail("search_document", "timeout", "took too long")}), "find x")
    assert "reported a problem (timeout)" in broken.text and broken.status is RunStatus.COMPLETED


# ----------------------------------------------------------------------------- detect and propose
def test_detect_summarises_by_category_saves_nothing_and_passes_the_requested_categories():
    tools = FakeTools({"detect_sensitive_entities": ok("d", detection(candidate(1), candidate(2), candidate(3, "PHONE"), notes=["The NER model is not installed."]))})
    text = run(tools, "detect email, phone").text
    assert tools.calls[0][1]["categories"] == ["EMAIL", "PHONE"]
    assert "3 candidate(s): EMAIL 2, PHONE 1" in text and "nothing was saved" in text and "Note: The NER model is not installed." in text
    assert [c[0] for c in tools.calls] == ["detect_sensitive_entities"]  # detect never proposes


def test_an_unknown_category_is_explained_without_calling_any_tool():
    tools = FakeTools({})
    text = run(tools, "detect passports").text
    assert "do not know the category 'passports'" in text and tools.calls == []


def test_propose_saves_what_was_detected_without_coordinates_and_only_as_a_proposal():
    tools = FakeTools(
        {
            "detect_sensitive_entities": ok("d", detection(candidate(1), candidate(2))),
            "propose_redactions": ok("p", {"created": [{"finding_id": "f1"}, {"finding_id": "f2"}], "already_existed": [], "refused": []}),
        }
    )
    result = run(tools, "propose")
    detect_call, propose_call = tools.calls
    assert detect_call[1]["categories"] == DEFAULT_PROPOSE  # company names stay visible by default
    items = propose_call[1]["proposals"]
    assert [i["expected_text"] for i in items] == ["x1@example.com", "x2@example.com"]
    assert all(set(i) == {"page_number", "char_start", "char_end", "category", "expected_text", "explanation"} for i in items)
    assert "Proposed 2 new redaction(s)" in result.text and "nothing is approved or redacted until you approve" in result.text
    assert "Not proposed: ADDRESS, LOCATION, ORGANIZATION" in result.text
    assert [e.tool for e in result.tool_events] == ["detect_sensitive_entities", "propose_redactions"]


def test_naming_categories_replaces_the_default_and_drops_the_left_out_note():
    tools = FakeTools(
        {
            "detect_sensitive_entities": ok("d", detection(candidate(1, "ORGANIZATION"))),
            "propose_redactions": ok("p", {"created": [{"finding_id": "f1"}], "already_existed": [], "refused": []}),
        }
    )
    text = run(tools, "propose organization").text
    assert tools.calls[0][1]["categories"] == ["ORGANIZATION"] and "Not proposed" not in text


def test_a_large_detection_is_proposed_in_batches_the_server_accepts_and_the_totals_add_up():
    many = [candidate(n) for n in range(MAX_PROPOSALS_PER_CALL * 2 + 20)]
    sizes: list[int] = []

    def propose(args):
        sizes.append(len(args["proposals"]))
        return ok("p", {"created": args["proposals"][:-1], "already_existed": args["proposals"][-1:], "refused": []})

    tools = FakeTools({"detect_sensitive_entities": ok("d", detection(*many)), "propose_redactions": propose})
    text = run(tools, "propose").text
    assert sizes == [MAX_PROPOSALS_PER_CALL, MAX_PROPOSALS_PER_CALL, 20]
    assert f"Proposed {len(many) - 3} new redaction(s)" in text and "3 already existed" in text


def test_propose_with_nothing_detected_or_a_failing_tool_says_so():
    nothing = FakeTools({"detect_sensitive_entities": ok("d", detection())})
    assert "Nothing to propose" in run(nothing, "propose").text and [c[0] for c in nothing.calls] == ["detect_sensitive_entities"]
    failing = FakeTools({"detect_sensitive_entities": ok("d", detection(candidate(1))), "propose_redactions": fail("p", "invalid_arguments", "bad")})
    assert "propose_redactions tool reported a problem (invalid_arguments)" in run(failing, "propose").text


# ----------------------------------------------------------------------------- policy
POLICY_OK = ok(
    "r",
    {
        "status": "ok", "message": "ok",
        "passages": [{"citation_id": PASSAGE_ID, "policy_title": "Northwind Redaction Policy", "section": "6. Organisation names", "score": 0.5123, "text": PASSAGE_TEXT}],
    },
)  # fmt: skip


def test_policy_quotes_the_first_sentence_with_its_citation_and_every_citation_verifies():
    result = run(FakeTools({"retrieve_policy_guidance": POLICY_OK}), "policy can company names stay visible?")
    assert "Northwind Redaction Policy, 6. Organisation names (similarity 0.51)" in result.text
    assert f"“Company names remain visible unless a reviewer asks otherwise.” [{PASSAGE_ID}]" in result.text
    citations, unverified = verify_citations(result.text, result.evidence)
    assert unverified == 0 and [(c.ref, c.verified, bool(c.quote)) for c in citations] == [(PASSAGE_ID, True, True)]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "no_relevant_policy", "message": "No passage was similar enough to your question.", "passages": []},
        {"status": "no_policies", "message": "No active policies are loaded.", "passages": []},
        {"status": "unavailable", "message": "Semantic search is unavailable: the model is not downloaded.", "passages": []},
    ],
)
def test_policy_without_passages_repeats_the_tools_message_and_never_claims_the_policy_is_silent(payload: dict):
    text = run(FakeTools({"retrieve_policy_guidance": ok("r", payload)}), "policy anything").text
    assert payload["message"] in text and "does not prove the policy is silent" in text and "“" not in text


def test_policy_needs_a_question():
    tools = FakeTools({})
    assert "Ask a question" in run(tools, "policy").text and tools.calls == []


# ----------------------------------------------------------------------------- summary and export
def test_summary_reports_status_blockers_and_warnings():
    data = {"summary": {"by_status": {"proposed": 3, "approved": 1}, "unresolved": 3, "approved": 1, "can_export": False,
                        "export_blockers": ["3 proposal(s) are undecided."], "warnings": ["Pages 2 contain images."]}}  # fmt: skip
    text = run(FakeTools({"get_review_summary": ok("s", data)}), "summary").text
    assert "1 approved, 3 proposed" in text and "Undecided: 3. Approved: 1." in text
    assert "Blocked: 3 proposal(s) are undecided." in text and "Warning: Pages 2 contain images." in text


def test_export_without_a_reviewer_authorization_explains_the_hand_off():
    err = fail("e", "tool_error", "authorization_required: No export authorization from the reviewer is pending.")
    text = run(FakeTools({"export_approved_redactions": err}), "export").text
    assert "click “Authorize export” in the Export tab" in text and "5 minutes" in text


def test_export_after_authorization_reports_the_result():
    data = {"status": "verified", "approved_count": 2, "downloadable": True, "next_step": "The reviewer downloads the file from the Export tab."}
    text = run(FakeTools({"export_approved_redactions": ok("e", data)}), "export").text
    assert "Export verified: 2 approved redaction(s); ready to download in the Export tab." in text and "The reviewer downloads the file" in text


# ----------------------------------------------------------------------------- limits and cancellation
def test_cancel_during_a_tool_call_stops_the_command():
    hang = Hang()
    cancel = asyncio.Event()

    async def scenario() -> LoopResult:
        task = asyncio.create_task(DemoAssistant(FakeTools({"search_document": hang})).run(document_id=DOC, history=[], user_text="find x", cancel=cancel))
        await asyncio.sleep(0.1)
        cancel.set()
        result = await asyncio.wait_for(task, timeout=3)
        await asyncio.sleep(0)
        assert hang.was_cancelled
        return result

    result = asyncio.run(scenario())
    assert result.status is RunStatus.CANCELLED and "Stopped at your request" in result.text


def test_the_time_limit_applies_to_demo_commands_too():
    hang = Hang()
    started = time.monotonic()
    result = run(FakeTools({"search_document": hang}), "find x", limits=AgentLimits(max_seconds=0.2), after=lambda: None)
    assert time.monotonic() - started < 3 and hang.was_cancelled
    assert result.limits_hit == ["max_seconds"] and "time limit" in result.text


def test_tool_events_carry_only_counts_never_document_text():
    data = {"returned": 1, "truncated": False, "occurrences": [{"page_number": 2, "context": "SECRET-NAME Jane Q. Sentinel"}], "unsupported_pages": []}
    result = run(FakeTools({"search_document": ok("s", data)}), "find SECRET-NAME")
    assert all("SECRET" not in e.summary and "Sentinel" not in e.summary for e in result.tool_events)


# ============================================================================= the real tool server, end to end
def say(client: TestClient, cid: str, text: str) -> dict:
    run_id = client.post(f"/api/conversations/{cid}/messages", json={"content": text}).json()["run"]["id"]
    deadline = time.monotonic() + 90
    while True:
        detail = client.get(f"/api/runs/{run_id}").json()
        if detail["run"]["status"] in ("completed", "failed", "cancelled"):
            return detail
        assert time.monotonic() < deadline, "run did not finish"
        time.sleep(0.1)


def start(client: TestClient, data: bytes) -> tuple[str, str]:
    doc = upload(client, data, "doc.pdf").json()["id"]
    return doc, client.post(f"/api/documents/{doc}/conversations", json={}).json()["id"]


def test_demo_commands_run_the_real_tools_and_the_reviewer_stays_in_control(client: TestClient, sample_contract_bytes: bytes):
    doc, cid = start(client, sample_contract_bytes)

    assert "No AI model is involved" in say(client, cid, "help")["assistant_message"]["content"]
    found = say(client, cid, "find Maria")
    assert found["run"]["mode"] == "demo" and found["run"]["provider"] is None
    assert "match(es) for “Maria”" in found["assistant_message"]["content"] and found["tool_events"][0]["tool"] == "search_document"

    detected = say(client, cid, "detect email")
    assert "EMAIL" in detected["assistant_message"]["content"] and client.get(f"/api/documents/{doc}/findings").json()["items"] == []

    proposed = say(client, cid, "propose email")
    stored = client.get(f"/api/documents/{doc}/findings").json()["items"]
    assert stored and all(f["status"] == "proposed" and f["origin"] == "agent" for f in stored)  # proposals only
    assert f"Proposed {len(stored)} new redaction(s)" in proposed["assistant_message"]["content"]

    again = say(client, cid, "propose email")
    assert "Proposed 0 new redaction(s)" in again["assistant_message"]["content"] and len(client.get(f"/api/documents/{doc}/findings").json()["items"]) == len(stored)

    summary = say(client, cid, "summary")["assistant_message"]["content"]
    assert f"Undecided: {len(stored)}. Approved: 0." in summary and "Blocked:" in summary

    refused = say(client, cid, "export")["assistant_message"]["content"]  # nothing approved, nothing authorized
    assert "cannot create the redacted file yet" in refused and client.get(f"/api/documents/{doc}/exports").json() == []


def test_the_assistants_export_works_only_after_the_reviewer_approves_and_authorizes(client: TestClient, sample_contract_bytes: bytes):
    from tests.test_export_api import authorize

    doc, cid = start(client, sample_contract_bytes)
    say(client, cid, "propose email")
    assert "cannot create the redacted file yet" in say(client, cid, "export")["assistant_message"]["content"]

    ids = [f["id"] for f in client.get(f"/api/documents/{doc}/findings").json()["items"]]  # the REVIEWER approves, through the REST API
    client.post(f"/api/documents/{doc}/findings/bulk-decision", json={"action": "approve", "finding_ids": ids})
    assert "cannot create the redacted file yet" in say(client, cid, "export")["assistant_message"]["content"]  # approved, but no authorization yet

    authorize(client, doc)  # ...and the REVIEWER authorizes
    exported = say(client, cid, "export")["assistant_message"]["content"]
    assert "Export verified" in exported and "ready to download" in exported
    records = client.get(f"/api/documents/{doc}/exports").json()
    assert len(records) == 1 and records[0]["status"] == "verified"
    assert "cannot create the redacted file yet" in say(client, cid, "export")["assistant_message"]["content"] or len(client.get(f"/api/documents/{doc}/exports").json()) == 1  # single-use


@requires_embedding_model
def test_demo_policy_answers_quote_real_passages_and_every_citation_verifies(client: TestClient, sample_contract_bytes: bytes):
    assert client.post("/api/policies/seed-samples", json={}).status_code == 200
    _, cid = start(client, sample_contract_bytes)
    detail = say(client, cid, "policy do we have to redact the names of individuals?")
    message = detail["assistant_message"]
    assert message["citations"], message["content"]
    assert all(c["verified"] and c["kind"] == "policy" and c["quote"] for c in message["citations"])
    assert detail["run"]["unverified_citations"] == 0 and detail["tool_events"][0]["tool"] == "retrieve_policy_guidance"


def test_a_document_without_text_still_gets_an_honest_reply(client: TestClient):
    _, cid = start(client, make_text_pdf([["plain words only"]]))
    nothing = say(client, cid, "detect email")["assistant_message"]["content"]
    assert "found nothing" in nothing


@pytest.mark.parametrize("command", ["find x", "detect", "propose", "policy x", "summary", "export", "find"])
def test_every_kind_of_reply_is_labelled_as_scripted_even_when_the_tool_fails(command: str):
    broken = FakeTools({n: fail(n, "timeout", "slow") for n in ("search_document", "detect_sensitive_entities", "retrieve_policy_guidance", "get_review_summary", "export_approved_redactions", "propose_redactions")})
    assert run(broken, command).text.rstrip().endswith("No AI model was used.)")


def test_the_detectors_generic_suggestion_note_is_not_repeated_after_a_propose_but_specific_notes_are():
    notes = ["Candidates are suggestions only. Nothing is proposed or redacted until you choose it.", "The NER model is not installed."]
    tools = FakeTools(
        {
            "detect_sensitive_entities": ok("d", detection(candidate(1), notes=notes)),
            "propose_redactions": ok("p", {"created": [{"finding_id": "f1"}], "already_existed": [], "refused": []}),
        }
    )
    text = run(tools, "propose").text
    assert "Nothing is proposed or redacted until you choose it" not in text and "Note: The NER model is not installed." in text


def test_demo_commands_report_each_tool_call_as_it_happens():
    delivered = []

    async def on_event(event):
        delivered.append(event.tool)

    tools = FakeTools(
        {
            "detect_sensitive_entities": ok("d", detection(candidate(1))),
            "propose_redactions": ok("p", {"created": [{"finding_id": "f1"}], "already_existed": [], "refused": []}),
        }
    )

    async def scenario():
        return await DemoAssistant(tools).run(document_id=DOC, history=[], user_text="propose", on_tool_event=on_event)

    result = asyncio.run(scenario())
    assert delivered == ["detect_sensitive_entities", "propose_redactions"] == [e.tool for e in result.tool_events]
