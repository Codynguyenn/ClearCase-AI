"""Review workflow tests: proposals, decisions, manual regions, adjustments, summary, staleness."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import FINDINGS, PAGES, REVIEW_REVISIONS
from app.models.findings import Category, Origin, ProposalInput, ReviewLabel
from app.repositories.review import FindingRepository
from app.services.errors import NotFoundError
from app.services.review_service import ReviewService
from tests.conftest import make_text_pdf, upload

LINES = [
    [
        "Contact Alice Smith at alice@example.com today.",
        "Alice Smith signed. ALICE SMITH again.",
        "Backup mail: bob@example.org",
    ]
]


def new_doc(client: TestClient, lines=None) -> str:
    doc = upload(client, make_text_pdf(lines or LINES)).json()
    assert client.get(f"/api/documents/{doc['id']}").json()["status"] == "ready"
    return doc["id"]


def propose_emails(client: TestClient, doc_id: str) -> dict:
    r = client.post(f"/api/documents/{doc_id}/findings/propose-detected", json={"categories": ["EMAIL"]})
    assert r.status_code == 201, r.text
    return r.json()


def revision(client: TestClient, doc_id: str) -> int:
    return client.get(f"/api/documents/{doc_id}").json()["review_revision"]


# -------------------------------------------------------------------- proposing


def test_detection_proposals_are_never_pre_approved(client: TestClient):
    doc_id = new_doc(client)
    result = propose_emails(client, doc_id)
    assert len(result["created"]) == 2
    assert {f["status"] for f in result["created"]} == {"proposed"}
    first = result["created"][0]
    assert first["category"] == "EMAIL" and first["origin"] == "detector"
    assert first["source"].startswith("pattern:") and first["label"] in ("format_valid", "review_needed")
    assert first["boxes"] and first["mappable"] is True


def test_proposing_twice_is_idempotent(client: TestClient):
    doc_id = new_doc(client)
    first = propose_emails(client, doc_id)
    rev_after_first = revision(client, doc_id)
    second = propose_emails(client, doc_id)
    assert second["created"] == []
    assert {f["id"] for f in second["existing"]} == {f["id"] for f in first["created"]}
    assert revision(client, doc_id) == rev_after_first  # nothing changed -> no bump
    assert client.get(f"/api/documents/{doc_id}/findings").json()["total"] == 2


def test_propose_search_requires_an_explicit_selection(client: TestClient):
    doc_id = new_doc(client)
    url = f"/api/documents/{doc_id}/findings/propose-search"
    r = client.post(url, json={"q": "Alice Smith"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "selection_required"

    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    assert len(occ) == 3  # case-insensitive
    r = client.post(url, json={"q": "Alice Smith", "occurrence_ids": [occ[0]["occurrence_id"]]})
    assert r.status_code == 201 and len(r.json()["created"]) == 1
    assert r.json()["created"][0]["category"] == "CUSTOM"
    assert r.json()["created"][0]["status"] == "proposed"

    r = client.post(url, json={"q": "Alice Smith", "all_matches": True})
    assert len(r.json()["created"]) == 2 and len(r.json()["existing"]) == 1


def test_unknown_occurrence_ids_are_reported_not_ignored(client: TestClient):
    doc_id = new_doc(client)
    r = client.post(
        f"/api/documents/{doc_id}/findings/propose-search", json={"q": "Alice", "occurrence_ids": ["occ_nope"]}
    )
    assert r.status_code == 201
    assert r.json()["created"] == [] and r.json()["rejected_inputs"]


def test_server_refuses_proposals_whose_expected_text_changed(client: TestClient):
    doc_id = new_doc(client)
    service = ReviewService(client.app.state.mongo, _settings(client))
    start = LINES[0][0].index("Alice")
    bad = ProposalInput(
        page_number=1, char_start=0, char_end=7, category=Category.PERSON, source="test",
        origin=Origin.DETECTOR, label=ReviewLabel.REVIEW_NEEDED, expected_text="Alice S",
    )
    good = ProposalInput(
        page_number=1, char_start=start, char_end=start + 11, category=Category.PERSON, source="test",
        origin=Origin.DETECTOR, label=ReviewLabel.REVIEW_NEEDED, expected_text="Alice Smith",
    )
    result = service.propose(doc_id, _settings(client).demo_owner_id, [bad, good])
    assert len(result.created) == 1 and result.created[0].text == "Alice Smith"
    assert len(result.rejected_inputs) == 1 and "no longer matches" in result.rejected_inputs[0]


def _settings(client: TestClient):
    from app.config import get_settings

    return client.app.dependency_overrides[get_settings]()


def test_out_of_range_spans_are_refused(client: TestClient):
    doc_id = new_doc(client)
    service = ReviewService(client.app.state.mongo, _settings(client))
    owner = _settings(client).demo_owner_id
    oob = ProposalInput(
        page_number=1, char_start=0, char_end=99999, category=Category.PERSON, source="t",
        origin=Origin.DETECTOR, label=ReviewLabel.REVIEW_NEEDED,
    )
    missing_page = oob.model_copy(update={"page_number": 7, "char_end": 5})
    result = service.propose(doc_id, owner, [oob, missing_page])
    assert result.created == [] and len(result.rejected_inputs) == 2


# --------------------------------------------------------------------- deciding


def test_decisions_change_status_and_bump_revision(client: TestClient):
    doc_id = new_doc(client)
    created = propose_emails(client, doc_id)["created"]
    rev = revision(client, doc_id)
    fid = created[0]["id"]

    r = client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    assert r.status_code == 200
    assert r.json()["updated"][0]["status"] == "approved"
    assert r.json()["review_revision"] == rev + 1 == revision(client, doc_id)

    # Repeating the same decision changes nothing.
    r = client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    assert r.json()["updated"] == [] and revision(client, doc_id) == rev + 1

    r = client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "reopen"})
    assert r.json()["updated"][0]["status"] == "proposed"


def test_apply_to_all_matching_is_explicit(client: TestClient):
    doc_id = new_doc(client)
    client.post(f"/api/documents/{doc_id}/findings/propose-search", json={"q": "Alice Smith", "all_matches": True})
    items = client.get(f"/api/documents/{doc_id}/findings").json()["items"]
    assert len(items) == 3
    # "Alice Smith" and "ALICE SMITH" normalise to the same group; each decision is still per finding unless asked.
    one = items[0]
    r = client.post(f"/api/documents/{doc_id}/findings/{one['id']}/decision", json={"action": "approve"})
    assert len(r.json()["updated"]) == 1
    r = client.post(
        f"/api/documents/{doc_id}/findings/{one['id']}/decision",
        json={"action": "approve", "apply_to_all_matching": True},
    )
    approved = client.get(f"/api/documents/{doc_id}/findings", params={"status": "approved"}).json()["total"]
    assert approved == len({f["id"] for f in items if f["group_key"] == one["group_key"]})
    assert approved >= 2


def test_bulk_decision_and_unknown_ids(client: TestClient):
    doc_id = new_doc(client)
    ids = [f["id"] for f in propose_emails(client, doc_id)["created"]]
    r = client.post(f"/api/documents/{doc_id}/findings/bulk-decision", json={"action": "reject", "finding_ids": ids})
    assert r.status_code == 200 and {f["status"] for f in r.json()["updated"]} == {"rejected"}
    r = client.post(
        f"/api/documents/{doc_id}/findings/bulk-decision", json={"action": "approve", "finding_ids": ids + ["fnd_x"]}
    )
    assert r.status_code == 404


def test_unmappable_finding_cannot_be_approved_until_adjusted(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    fid = propose_emails(client, doc_id)["created"][0]["id"]
    db = mongo_client[test_settings.mongodb_db]
    db[FINDINGS].update_one({"_id": fid}, {"$set": {"mappable": False, "boxes": []}})
    r = client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "needs_manual_review"

    r = client.put(
        f"/api/documents/{doc_id}/findings/{fid}/boxes", json={"boxes": [{"x0": 10, "y0": 10, "x1": 100, "y1": 30}]}
    )
    assert r.status_code == 200 and r.json()["mappable"] is True
    r = client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    assert r.status_code == 200


# ---------------------------------------------------------------- manual/adjust


def test_manual_region_is_created_approved_and_validated(client: TestClient):
    doc_id = new_doc(client)
    url = f"/api/documents/{doc_id}/findings/manual"
    box = {"x0": 40, "y0": 100, "x1": 200, "y1": 140}
    r = client.post(url, json={"page_number": 1, "box": box})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "approved" and body["category"] == "MANUAL" and body["char_start"] is None
    again = client.post(url, json={"page_number": 1, "box": box})
    assert again.json()["id"] == body["id"]  # idempotent

    for bad, code in [
        ({"x0": 10, "y0": 10, "x1": 900, "y1": 40}, "box_outside_page"),
        ({"x0": 50, "y0": 10, "x1": 20, "y1": 40}, "invalid_box"),
        ({"x0": 10, "y0": 10, "x1": 10.2, "y1": 40}, "box_too_small"),
    ]:
        r = client.post(url, json={"page_number": 1, "box": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, bad
    assert client.post(url, json={"page_number": 5, "box": box}).status_code == 404


def test_adjusting_resets_an_approved_finding_to_proposed(client: TestClient):
    doc_id = new_doc(client)
    fid = propose_emails(client, doc_id)["created"][0]["id"]
    client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    rev = revision(client, doc_id)
    r = client.put(
        f"/api/documents/{doc_id}/findings/{fid}/boxes", json={"boxes": [{"x0": 20, "y0": 20, "x1": 120, "y1": 50}]}
    )
    body = r.json()
    assert body["status"] == "proposed" and body["adjusted"] is True and body["decided_at"] is None
    assert body["boxes"] == [{"x0": 20, "y0": 20, "x1": 120, "y1": 50}]
    assert body["original_boxes"] != body["boxes"]
    assert revision(client, doc_id) == rev + 1


# ------------------------------------------------------------------- list/summary


def test_list_filters_and_overlaps(client: TestClient):
    doc_id = new_doc(client)
    propose_emails(client, doc_id)
    client.post(f"/api/documents/{doc_id}/findings/propose-search", json={"q": "alice@example.com", "all_matches": True})
    everything = client.get(f"/api/documents/{doc_id}/findings").json()
    assert everything["total"] == 3
    emails = [f for f in everything["items"] if f["category"] == "EMAIL" and "alice@" in f["text"]][0]
    custom = [f for f in everything["items"] if f["category"] == "CUSTOM"][0]
    assert custom["id"] in emails["overlaps"] and emails["id"] in custom["overlaps"]  # kept, both flagged

    only_custom = client.get(f"/api/documents/{doc_id}/findings", params={"category": "CUSTOM"}).json()
    assert [f["id"] for f in only_custom["items"]] == [custom["id"]]
    assert client.get(f"/api/documents/{doc_id}/findings", params={"page": 2}).json()["total"] == 0
    assert client.get(f"/api/documents/{doc_id}/findings", params={"status": "bogus"}).status_code == 422


def test_summary_blockers_follow_review_state(client: TestClient):
    doc_id = new_doc(client)
    url = f"/api/documents/{doc_id}/review-summary"
    s = client.get(url).json()
    assert s["can_export"] is False and any("No approved" in b for b in s["export_blockers"])

    ids = [f["id"] for f in propose_emails(client, doc_id)["created"]]
    client.post(f"/api/documents/{doc_id}/findings/{ids[0]}/decision", json={"action": "approve"})
    s = client.get(url).json()
    assert s["can_export"] is False and s["unresolved"] == 1
    assert any("undecided" in b for b in s["export_blockers"])

    client.post(f"/api/documents/{doc_id}/findings/{ids[1]}/decision", json={"action": "reject"})
    s = client.get(url).json()
    assert s["can_export"] is True and s["approved"] == 1 and s["export_blockers"] == []
    assert s["by_status"] == {"approved": 1, "rejected": 1}
    assert s["by_category"]["EMAIL"] == {"approved": 1, "rejected": 1}


def test_summary_warns_about_unproposed_copies_without_leaking_text(client: TestClient):
    doc_id = new_doc(client)
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    r = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [occ[0]["occurrence_id"]]},
    )
    fid = r.json()["created"][0]["id"]
    client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    s = client.get(f"/api/documents/{doc_id}/review-summary").json()
    assert s["unproposed_copies"] == 2
    assert any("not covered" in w for w in s["warnings"])
    assert "Alice" not in str(s)  # counts and pages only


def test_summary_notes_unsupported_pages(client: TestClient):
    from tests.conftest import make_mixed_pdf

    doc = upload(client, make_mixed_pdf()).json()
    s = client.get(f"/api/documents/{doc['id']}/review-summary").json()
    assert s["unsupported_pages"] == [1]
    assert any("scanned or mixed" in w for w in s["warnings"])


# ------------------------------------------------------- revisions, isolation, etc


def test_revision_log_is_append_only_and_text_free(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    fid = propose_emails(client, doc_id)["created"][0]["id"]
    client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    entries = list(mongo_client[test_settings.mongodb_db][REVIEW_REVISIONS].find({}, {"_id": 0}))
    assert [e["revision"] for e in entries] == [1, 2]
    assert [e["action"] for e in entries][0] == "propose_detected"
    assert "alice@example.com" not in str(entries)


def test_findings_are_scoped_to_their_owner(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    fid = propose_emails(client, doc_id)["created"][0]["id"]
    repo = FindingRepository(mongo_client[test_settings.mongodb_db])
    assert repo.get(doc_id, test_settings.demo_owner_id, fid)
    with pytest.raises(NotFoundError):
        repo.get(doc_id, "someone-else", fid)
    assert repo.list(doc_id, "someone-else") == []


def test_findings_endpoints_404_for_unknown_document(client: TestClient):
    for method, path, body in [
        ("get", "/api/documents/000000000000000000000000/findings", None),
        ("get", "/api/documents/000000000000000000000000/review-summary", None),
        ("post", "/api/documents/000000000000000000000000/findings/propose-detected", {"categories": ["EMAIL"]}),
    ]:
        r = getattr(client, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 404, path


def test_reprocess_keeps_matching_findings_and_stales_changed_ones(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    ids = [f["id"] for f in propose_emails(client, doc_id)["created"]]
    rev = revision(client, doc_id)
    db = mongo_client[test_settings.mongodb_db]
    # Simulate "the stored text for this finding no longer matches what re-extraction yields".
    db[FINDINGS].update_one({"_id": ids[0]}, {"$set": {"text": "something else"}})

    assert client.post(f"/api/documents/{doc_id}/reprocess").json()["status"] == "ready"
    by_id = {f["id"]: f for f in client.get(f"/api/documents/{doc_id}/findings").json()["items"]}
    assert by_id[ids[0]]["status"] == "stale"
    assert by_id[ids[1]]["status"] == "proposed" and by_id[ids[1]]["document_version"] == 2
    assert revision(client, doc_id) > rev

    r = client.post(f"/api/documents/{doc_id}/findings/{ids[0]}/decision", json={"action": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_finding"
    s = client.get(f"/api/documents/{doc_id}/review-summary").json()
    assert any("stale" in w for w in s["warnings"])


def test_delete_cascades_to_findings_and_revisions(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    fid = propose_emails(client, doc_id)["created"][0]["id"]
    client.post(f"/api/documents/{doc_id}/findings/{fid}/decision", json={"action": "approve"})
    deleted = client.delete(f"/api/documents/{doc_id}").json()["deleted"]
    assert deleted["findings"] == 2 and deleted["review_revisions"] == 2
    db = mongo_client[test_settings.mongodb_db]
    assert db[FINDINGS].count_documents({}) == 0 and db[REVIEW_REVISIONS].count_documents({}) == 0
    assert db[PAGES].count_documents({}) == 0


def test_detect_preview_stores_nothing(client: TestClient):
    doc_id = new_doc(client)
    r = client.post(f"/api/documents/{doc_id}/detect", json={"categories": ["EMAIL"]})
    assert r.status_code == 200 and len(r.json()["candidates"]) == 2
    assert client.get(f"/api/documents/{doc_id}/findings").json()["total"] == 0
    assert revision(client, doc_id) == 0
