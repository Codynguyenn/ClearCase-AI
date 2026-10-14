"""Export filename privacy, and "verified with warnings" (reviewer acknowledgement before download).

Two promises are tested here:

* The downloaded file's name never contains the uploaded file's name (which may itself hold exactly
  the sensitive text that was redacted).
* If copies of approved text are still readable in the output, the export is "verified with warnings"
  and the reviewer must acknowledge that before the file can be downloaded. The assistant cannot.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from gridfs import GridFSBucket

from app.config import Settings
from app.db import AUDIT_EVENTS, EXPORTS, PDF_BUCKET
from app.main import create_app
from app.services import export_service
from app.services.export_service import neutral_export_filename
from tests.conftest import make_text_pdf, upload
from tests.test_export_api import LINES, approve_all_emails, authorize, do_export, new_doc

NEUTRAL = re.compile(r"^redacted_[0-9a-f]{10}\.pdf$")


def export_with_one_copy_left(client: TestClient, doc_id: str) -> dict:
    """Approve only the first of two "Alice Smith" mentions, so one readable copy remains."""
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    created = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [occ[0]["occurrence_id"]]},
    ).json()["created"]
    client.post(f"/api/documents/{doc_id}/findings/{created[0]['id']}/decision", json={"action": "approve"})
    return do_export(client, doc_id, authorize(client, doc_id)).json()


def file_url(doc_id: str, export_id: str) -> str:
    return f"/api/documents/{doc_id}/exports/{export_id}/file"


def ack_url(doc_id: str, export_id: str) -> str:
    return f"/api/documents/{doc_id}/exports/{export_id}/acknowledge-warnings"


# ------------------------------------------------------------------------ file names
def test_the_filename_is_derived_from_the_output_hash_only():
    assert neutral_export_filename("0123456789abcdef" * 4) == "redacted_0123456789.pdf"
    assert NEUTRAL.match(neutral_export_filename("f" * 64))


def test_the_download_name_never_contains_the_uploaded_name(client: TestClient, mongo_client, test_settings: Settings):
    sensitive_name = "Maria Delgado-Reyes NDA.pdf"
    doc_id = upload(client, make_text_pdf(LINES), filename=sensitive_name).json()["id"]
    approve_all_emails(client, doc_id)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()

    assert NEUTRAL.match(export["filename"])
    dl = client.get(file_url(doc_id, export["id"]))
    disposition = dl.headers["content-disposition"]
    assert NEUTRAL.match(re.search(r'filename="([^"]+)"', disposition).group(1))
    for fragment in ("Maria", "Delgado", "Reyes", "NDA"):
        assert fragment not in disposition and fragment not in export["filename"]

    # Nor in the stored file's own name inside GridFS.
    bucket = GridFSBucket(mongo_client[test_settings.mongodb_db], bucket_name=PDF_BUCKET)
    stored = [f.filename for f in bucket.find({"metadata.kind": "export"})]
    assert stored == [export["filename"]]


def test_exports_stored_before_this_rule_are_still_served_under_the_neutral_name(
    client: TestClient, mongo_client, test_settings: Settings
):
    doc_id = upload(client, make_text_pdf(LINES), filename="Maria Delgado-Reyes NDA.pdf").json()["id"]
    approve_all_emails(client, doc_id)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    # Simulate an older record that stored the name derived from the upload.
    mongo_client[test_settings.mongodb_db][EXPORTS].update_many({}, {"$set": {"filename": "Maria Delgado-Reyes NDA_redacted.pdf"}})

    listed = client.get(f"/api/documents/{doc_id}/exports").json()[0]
    assert listed["filename"] == export["filename"] and NEUTRAL.match(listed["filename"])
    assert "Maria" not in client.get(file_url(doc_id, export["id"])).headers["content-disposition"]


# --------------------------------------------------- verified with warnings: gating
def test_an_export_with_remaining_copies_is_verified_with_warnings_and_cannot_be_downloaded_yet(client: TestClient):
    doc_id = new_doc(client)
    export = export_with_one_copy_left(client, doc_id)

    assert export["status"] == "verified"  # the required checks all passed
    assert export["has_warnings"] is True
    assert export["requires_acknowledgement"] is True and export["warnings_acknowledged"] is False
    assert export["downloadable"] is False

    r = client.get(file_url(doc_id, export["id"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "warnings_unacknowledged"
    message = r.json()["error"]["message"]
    assert "1 other copy" in message and "page(s) 1" in message
    assert "Alice" not in message  # never echoes the text


def test_a_refused_download_is_not_recorded_as_a_download(client: TestClient, mongo_client, test_settings: Settings):
    doc_id = new_doc(client)
    export = export_with_one_copy_left(client, doc_id)
    client.get(file_url(doc_id, export["id"]))
    events = [e["event"] for e in mongo_client[test_settings.mongodb_db][AUDIT_EVENTS].find({})]
    assert "export_downloaded" not in events


def test_acknowledging_unlocks_the_download_and_is_audited_with_counts_only(
    client: TestClient, mongo_client, test_settings: Settings
):
    doc_id = new_doc(client)
    export = export_with_one_copy_left(client, doc_id)

    r = client.post(ack_url(doc_id, export["id"]))
    assert r.status_code == 200
    acked = r.json()
    assert acked["warnings_acknowledged"] is True and acked["downloadable"] is True
    assert acked["has_warnings"] is True  # still honest about the warning after it was acknowledged
    assert client.get(file_url(doc_id, export["id"])).status_code == 200

    events = list(mongo_client[test_settings.mongodb_db][AUDIT_EVENTS].find({"event": "export_warnings_acknowledged"}, {"_id": 0}))
    assert len(events) == 1
    assert events[0]["details"] == {"export_id": export["id"], "unresolved_copies": 1, "pages": [1]}
    assert "Alice" not in str(events)


def test_acknowledging_twice_changes_nothing_and_is_audited_once(client: TestClient, mongo_client, test_settings: Settings):
    doc_id = new_doc(client)
    export = export_with_one_copy_left(client, doc_id)
    first = client.post(ack_url(doc_id, export["id"])).json()
    second = client.post(ack_url(doc_id, export["id"])).json()
    assert first == second
    assert mongo_client[test_settings.mongodb_db][AUDIT_EVENTS].count_documents({"event": "export_warnings_acknowledged"}) == 1


def test_a_clean_export_needs_no_acknowledgement(client: TestClient, mongo_client, test_settings: Settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert export["has_warnings"] is False and export["requires_acknowledgement"] is False and export["downloadable"] is True
    assert client.get(file_url(doc_id, export["id"])).status_code == 200
    # Acknowledging nothing is harmless and leaves no audit trail.
    assert client.post(ack_url(doc_id, export["id"])).status_code == 200
    assert mongo_client[test_settings.mongodb_db][AUDIT_EVENTS].count_documents({"event": "export_warnings_acknowledged"}) == 0


def test_covering_the_remaining_copy_removes_the_need_to_acknowledge(client: TestClient):
    doc_id = new_doc(client)
    export_with_one_copy_left(client, doc_id)
    pending = client.get(f"/api/documents/{doc_id}/findings", params={"status": "approved"}).json()["items"]
    # Now approve the second mention too, and export again.
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    created = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [o["occurrence_id"] for o in occ]},
    ).json()
    for f in created["created"]:
        client.post(f"/api/documents/{doc_id}/findings/{f['id']}/decision", json={"action": "approve"})
    assert pending  # the first mention was already approved
    second = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert second["retained"]["unresolved"] == 0
    assert second["has_warnings"] is False and second["requires_acknowledgement"] is False and second["downloadable"] is True


def test_other_advisory_warnings_are_shown_but_do_not_block_download(client: TestClient, mongo_client, test_settings: Settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    export = do_export(client, doc_id, authorize(client, doc_id)).json()
    col = mongo_client[test_settings.mongodb_db][EXPORTS]
    stored = col.find_one({})
    for c in stored["checks"]:
        if c["name"] == "no_collateral_text_loss":
            c["passed"] = False  # an advisory check failing, with no remaining copies
    col.update_one({"_id": stored["_id"]}, {"$set": {"checks": stored["checks"]}})
    now = client.get(f"/api/documents/{doc_id}/exports/{export['id']}").json()
    assert now["has_warnings"] is True and now["requires_acknowledgement"] is False and now["downloadable"] is True


# ------------------------------------------------------------------- who and when
def test_a_stale_or_failed_export_cannot_be_acknowledged(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    doc_id = new_doc(client)
    export = export_with_one_copy_left(client, doc_id)
    finding = client.get(f"/api/documents/{doc_id}/findings", params={"status": "approved"}).json()["items"][0]
    client.post(f"/api/documents/{doc_id}/findings/{finding['id']}/decision", json={"action": "reopen"})
    r = client.post(ack_url(doc_id, export["id"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_stale"

    monkeypatch.setattr(export_service, "build_redacted_pdf", lambda original, boxes: original)  # output keeps the secrets
    doc2 = new_doc(client)
    approve_all_emails(client, doc2)
    failed = do_export(client, doc2, authorize(client, doc2)).json()
    assert failed["status"] == "verification_failed"
    r = client.post(ack_url(doc2, failed["id"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "export_not_verified"


def test_another_owner_cannot_acknowledge_or_see_the_export(client: TestClient, test_settings: Settings):
    doc_id = new_doc(client)
    export = export_with_one_copy_left(client, doc_id)
    other = create_app(test_settings.model_copy(update={"demo_owner_id": "someone-else"}))
    with TestClient(other) as intruder:
        assert intruder.post(ack_url(doc_id, export["id"])).status_code == 404
        assert intruder.get(file_url(doc_id, export["id"])).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/exports/{export['id']}").json()["warnings_acknowledged"] is False


def test_the_acknowledge_route_is_reviewer_only_and_not_an_assistant_tool(client: TestClient):
    # No MCP tool name mentions acknowledgement: the set of tools is pinned in test_mcp_protocol.py. Here we
    # check the REST route exists exactly once and is a POST (so a GET link cannot trigger it).
    spec = client.app.openapi()["paths"]
    routes = {p: set(ops) for p, ops in spec.items() if "acknowledge-warnings" in p}
    assert routes == {"/api/documents/{document_id}/exports/{export_id}/acknowledge-warnings": {"post"}}
