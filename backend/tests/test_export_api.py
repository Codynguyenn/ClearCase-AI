"""End-to-end export tests: authorization rules, verification, storage, download, staleness."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.db import AUDIT_EVENTS, EXPORT_AUTHORIZATIONS, FINDINGS, PDF_BUCKET
from app.models.common import utcnow
from app.repositories.review import AuthorizationRepository
from app.services import export_service
from tests.conftest import make_mixed_pdf, make_text_pdf, upload

LINES = [
    [
        "Contact Alice Smith at alice@example.com today.",
        "Alice Smith signed. Reviewed by Carol Jones.",
        "Backup mail: bob@example.org",
    ]
]


def new_doc(client: TestClient, data: bytes | None = None) -> str:
    doc = upload(client, data or make_text_pdf(LINES)).json()
    assert client.get(f"/api/documents/{doc['id']}").json()["status"] in ("ready", "unsupported")
    return doc["id"]


def approve_all_emails(client: TestClient, doc_id: str) -> list[dict]:
    created = client.post(
        f"/api/documents/{doc_id}/findings/propose-detected", json={"categories": ["EMAIL"]}
    ).json()["created"]
    client.post(
        f"/api/documents/{doc_id}/findings/bulk-decision",
        json={"action": "approve", "finding_ids": [f["id"] for f in created]},
    )
    return created


def authorize(client: TestClient, doc_id: str) -> str:
    r = client.post(f"/api/documents/{doc_id}/export-authorizations")
    assert r.status_code == 201, r.text
    return r.json()["token"]


def do_export(client: TestClient, doc_id: str, token: str, **extra):
    return client.post(f"/api/documents/{doc_id}/exports", json={"authorization": token, **extra})


def pdf_text(data: bytes) -> str:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "".join(p.get_text() for p in doc)
    finally:
        doc.close()


# ---------------------------------------------------------------------- happy path


def test_full_export_flow_produces_a_verified_redacted_pdf(client: TestClient):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)

    token = authorize(client, doc_id)
    r = do_export(client, doc_id, token)
    assert r.status_code == 201, r.text
    export = r.json()
    assert export["status"] == "verified" and export["downloadable"] is True and export["stale"] is False
    assert export["approved_count"] == 2 and export["by_category"] == {"EMAIL": 2}
    names = {c["name"]: c for c in export["checks"]}
    assert all(c["passed"] for c in names.values() if c["required"]), export["checks"]
    assert {"text_removed_in_approved_boxes", "boxes_render_black", "metadata_and_attachments_scrubbed",
            "full_rewrite", "original_unchanged"} <= set(names)
    assert export["scope_statement"]

    dl = client.get(f"/api/documents/{doc_id}/exports/{export['id']}/file")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"
    # The name is derived from the output hash only: the uploaded file was called "test.pdf" and must not leak.
    assert f'attachment; filename="redacted_{export["output_hash"][:10]}.pdf"' == dl.headers["content-disposition"]
    assert export["filename"] == f"redacted_{export['output_hash'][:10]}.pdf"
    assert export["has_warnings"] is False and export["requires_acknowledgement"] is False
    assert hashlib.sha256(dl.content).hexdigest() == export["output_hash"]
    text = pdf_text(dl.content)
    assert "alice@example.com" not in text and "bob@example.org" not in text
    assert "Alice Smith" in text and "Carol Jones" in text  # only approved findings were removed
    assert b"alice@example.com" not in dl.content

    # The original is untouched and still downloadable.
    original = client.get(f"/api/documents/{doc_id}/file").content
    assert hashlib.sha256(original).hexdigest() == client.get(f"/api/documents/{doc_id}").json()["content_hash"]
    assert "alice@example.com" in pdf_text(original)


def test_export_cannot_be_authorized_until_review_is_complete(client: TestClient):
    doc_id = new_doc(client)
    r = client.post(f"/api/documents/{doc_id}/export-authorizations")
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_blocked"  # nothing approved

    client.post(f"/api/documents/{doc_id}/findings/propose-detected", json={"categories": ["EMAIL"]})
    r = client.post(f"/api/documents/{doc_id}/export-authorizations")
    assert r.status_code == 409 and "undecided" in r.json()["error"]["message"]  # proposals unresolved


# ------------------------------------------------------------ authorization rules


def test_authorization_is_single_use_and_idempotent(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    token = authorize(client, doc_id)
    first = do_export(client, doc_id, token).json()
    second = do_export(client, doc_id, token)
    assert second.status_code == 201 and second.json()["id"] == first["id"]
    assert len(client.get(f"/api/documents/{doc_id}/exports").json()) == 1
    db = mongo_client[test_settings.mongodb_db]
    assert db[f"{PDF_BUCKET}.files"].count_documents({"metadata.kind": "export"}) == 1


def test_atomic_consume_has_exactly_one_winner(mongo_client, test_settings):
    db = mongo_client[test_settings.mongodb_db]
    db[EXPORT_AUTHORIZATIONS].delete_many({})
    repo = AuthorizationRepository(db)
    repo.insert({"_id": "h", "document_id": "d", "owner_id": "o", "consumed_at": None, "export_id": None})
    results = [repo.consume("h", utcnow()) for _ in range(5)]
    assert results.count(True) == 1


def test_unknown_foreign_and_expired_tokens_are_refused(client: TestClient, mongo_client, test_settings):
    doc_a = new_doc(client)
    approve_all_emails(client, doc_a)
    doc_b = new_doc(client)
    approve_all_emails(client, doc_b)

    assert do_export(client, doc_a, "x" * 43).status_code == 403
    token_a = authorize(client, doc_a)
    r = do_export(client, doc_b, token_a)  # a token for document A cannot export document B
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_authorization"

    db = mongo_client[test_settings.mongodb_db]
    db[EXPORT_AUTHORIZATIONS].update_many({}, {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}})
    r = do_export(client, doc_a, token_a)
    # MongoDB's TTL monitor may already have deleted the expired token ("unknown"); both mean refused.
    assert r.status_code == 403
    assert r.json()["error"]["code"] in ("authorization_expired", "invalid_authorization")


def test_token_is_stored_only_as_a_hash(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    token = authorize(client, doc_id)
    stored = list(mongo_client[test_settings.mongodb_db][EXPORT_AUTHORIZATIONS].find())
    assert len(stored) == 1 and stored[0]["_id"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(stored)


def test_review_change_after_authorization_invalidates_it(client: TestClient):
    doc_id = new_doc(client)
    created = approve_all_emails(client, doc_id)
    token = authorize(client, doc_id)
    client.post(f"/api/documents/{doc_id}/findings/{created[0]['id']}/decision", json={"action": "reject"})
    r = do_export(client, doc_id, token)
    assert r.status_code == 409 and r.json()["error"]["code"] == "authorization_stale"
    assert client.get(f"/api/documents/{doc_id}/exports").json() == []


def test_changing_boxes_without_changing_ids_also_invalidates(client: TestClient, mongo_client, test_settings):
    """The digest covers the exact rectangles, not just the finding ids."""
    doc_id = new_doc(client)
    created = approve_all_emails(client, doc_id)
    token = authorize(client, doc_id)
    db = mongo_client[test_settings.mongodb_db]
    db[FINDINGS].update_one({"_id": created[0]["id"]}, {"$set": {"boxes.0.x1": 399}})  # out-of-band tampering
    r = do_export(client, doc_id, token)
    assert r.status_code == 409 and r.json()["error"]["code"] == "authorization_stale"


# ------------------------------------------------------------- verification gates


def test_failed_verification_stores_nothing_and_blocks_download(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mongo_client, test_settings
):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    # Sabotage the build step: "redaction" returns the untouched original.
    monkeypatch.setattr(export_service, "build_redacted_pdf", lambda original, boxes: original)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["status"] == "verification_failed" and export["downloadable"] is False
    failed = {c["name"] for c in export["checks"] if not c["passed"] and c["required"]}
    assert {"text_removed_in_approved_boxes", "boxes_render_black"} <= failed
    assert export["output_hash"] is None and export["filename"] is None
    db = mongo_client[test_settings.mongodb_db]
    assert db[f"{PDF_BUCKET}.files"].count_documents({"metadata.kind": "export"}) == 0
    r = client.get(f"/api/documents/{doc_id}/exports/{export['id']}/file")
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_not_verified"


def test_unexpected_crash_is_recorded_not_leaked(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)

    def boom(original, boxes):
        raise RuntimeError("secret internal detail alice@example.com")

    monkeypatch.setattr(export_service, "build_redacted_pdf", boom)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["status"] == "verification_failed"
    assert "alice@example.com" not in str(export)


def test_export_goes_stale_when_review_changes_later(client: TestClient):
    doc_id = new_doc(client)
    created = approve_all_emails(client, doc_id)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["downloadable"] is True

    client.post(f"/api/documents/{doc_id}/findings/{created[0]['id']}/decision", json={"action": "reopen"})
    now = client.get(f"/api/documents/{doc_id}/exports/{export['id']}").json()
    assert now["stale"] is True and now["downloadable"] is False
    r = client.get(f"/api/documents/{doc_id}/exports/{export['id']}/file")
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_stale"


def test_unproposed_copies_are_reported_but_not_a_hard_failure(client: TestClient):
    doc_id = new_doc(client)
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    created = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [occ[0]["occurrence_id"]]},
    ).json()["created"]
    client.post(f"/api/documents/{doc_id}/findings/{created[0]['id']}/decision", json={"action": "approve"})

    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["status"] == "verified"  # the reviewer approved only one occurrence
    assert export["retained"]["unresolved"] == 1 and export["retained"]["unresolved_locations"] == [
        {"page": 1, "group": 0}
    ]
    check = {c["name"]: c for c in export["checks"]}["no_unresolved_copies"]
    assert check["passed"] is False and check["required"] is False
    assert "Alice" not in str(export["retained"])


def test_rejected_findings_are_reported_as_intentionally_retained(client: TestClient):
    doc_id = new_doc(client)
    ids = [
        f["id"]
        for f in client.post(
            f"/api/documents/{doc_id}/findings/propose-search", json={"q": "Alice Smith", "all_matches": True}
        ).json()["created"]
    ]
    client.post(f"/api/documents/{doc_id}/findings/{ids[0]}/decision", json={"action": "approve"})
    client.post(f"/api/documents/{doc_id}/findings/{ids[1]}/decision", json={"action": "reject"})
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["retained"] == {"intentionally_retained": 1, "unresolved": 0, "unresolved_locations": []}


# ---------------------------------------------------- unsupported / blocked files


def test_unsupported_pages_require_acknowledgement(client: TestClient):
    doc_id = new_doc(client, make_mixed_pdf())
    client.post(
        f"/api/documents/{doc_id}/findings/manual",
        json={"page_number": 1, "box": {"x0": 40, "y0": 100, "x1": 200, "y1": 200}},
    )
    token = authorize(client, doc_id)
    r = do_export(client, doc_id, token)
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_pages_unacknowledged"
    assert client.get(f"/api/documents/{doc_id}/exports").json() == []  # not consumed by the refusal

    r = do_export(client, doc_id, token, acknowledge_unsupported_pages=True)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "verified" and body["unsupported_pages_acknowledged"] == [1]


def test_layered_pdf_is_blocked_with_a_clear_reason(client: TestClient):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), "layered content")
    doc.add_ocg("Hidden layer", on=False)
    data = doc.tobytes()
    doc.close()
    doc_id = new_doc(client, data)
    client.post(
        f"/api/documents/{doc_id}/findings/manual",
        json={"page_number": 1, "box": {"x0": 40, "y0": 30, "x1": 200, "y1": 60}},
    )
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["status"] == "blocked" and export["downloadable"] is False
    assert "layers" in export["checks"][0]["detail"]


# ------------------------------------------------------------- audit and cleanup


def test_audit_events_hold_ids_and_counts_only(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    client.get(f"/api/documents/{doc_id}/exports/{export['id']}/file")
    events = list(mongo_client[test_settings.mongodb_db][AUDIT_EVENTS].find({}, {"_id": 0}))
    assert [e["event"] for e in events] == ["export_authorized", "export_completed", "export_downloaded"]
    assert "alice@example.com" not in str(events) and "Alice" not in str(events)
    assert events[1]["details"]["status"] == "verified" and events[1]["details"]["failed_checks"] == []


def test_delete_removes_export_files_and_records(client: TestClient, mongo_client, test_settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    do_export(client, doc_id, authorize(client, doc_id))
    db = mongo_client[test_settings.mongodb_db]
    assert db[f"{PDF_BUCKET}.files"].count_documents({}) == 2  # original + export

    deleted = client.delete(f"/api/documents/{doc_id}").json()["deleted"]
    assert deleted["exports"] == 1 and deleted["export_files"] == 1 and deleted["export_authorizations"] == 1
    assert deleted["audit_events"] >= 2
    assert db[f"{PDF_BUCKET}.files"].count_documents({}) == 0
    assert db[f"{PDF_BUCKET}.chunks"].count_documents({}) == 0
    assert db[AUDIT_EVENTS].count_documents({}) == 0


def test_export_endpoints_404_for_unknown_ids(client: TestClient):
    doc_id = new_doc(client)
    assert client.get(f"/api/documents/{doc_id}/exports/not-an-id").status_code == 404
    assert client.get(f"/api/documents/{doc_id}/exports/000000000000000000000000/file").status_code == 404
    assert client.get("/api/documents/000000000000000000000000/exports").status_code == 404
