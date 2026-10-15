"""API tests against a real MongoDB: upload, processing states, pages, file, delete."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.db import DOCUMENTS, PAGES, PDF_BUCKET
from tests.conftest import make_encrypted_pdf, make_scanned_pdf, make_small_image_pdf, make_text_pdf, upload


def test_health_reports_mongo(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["mongodb"] is True
    assert body["pdf_engine"].startswith("pymupdf")
    assert "ner_available" in body


def test_upload_sample_contract_becomes_ready(client: TestClient, sample_contract_bytes: bytes):
    r = upload(client, sample_contract_bytes, "Sample Contract.pdf")
    assert r.status_code == 202, r.text
    created = r.json()
    assert created["status"] == "uploaded"
    assert created["page_count"] == 3
    assert created["content_hash"] == hashlib.sha256(sample_contract_bytes).hexdigest()
    assert created["filename"] == "Sample Contract.pdf"
    assert created["owner_id"] == "demo-user"

    # TestClient runs background tasks before returning, so the record is final now.
    r = client.get(f"/api/documents/{created['id']}")
    doc = r.json()
    assert doc["status"] == "ready"
    assert doc["extraction"]["supported_pages"] == 3
    assert doc["extraction"]["unsupported_pages"] == []
    assert doc["processing"]["attempts"] == 1
    assert doc["processing"]["error"] is None

    pages = client.get(f"/api/documents/{created['id']}/pages").json()
    assert [p["page_number"] for p in pages] == [1, 2, 3]
    assert all(p["kind"] == "text" and p["supported"] for p in pages)
    assert "text" not in pages[0]  # summaries omit the heavy fields

    page1 = client.get(f"/api/documents/{created['id']}/pages/1").json()
    assert "Maria Delgado-Reyes" in page1["text"]
    assert page1["geometry"] == {"width": 612.0, "height": 792.0, "rotation": 0}
    word = next(w for w in page1["words"] if w["text"].startswith("Álvarez"))
    assert page1["text"][word["char_start"] : word["char_end"]] == word["text"]


def test_list_documents_newest_first(client: TestClient):
    a = upload(client, make_text_pdf([["first"]]), "a.pdf").json()
    b = upload(client, make_text_pdf([["second"]]), "b.pdf").json()
    r = client.get("/api/documents")
    body = r.json()
    assert body["total"] == 2
    assert [d["id"] for d in body["items"]] == [b["id"], a["id"]]


def test_original_file_is_stored_unchanged(client: TestClient, sample_contract_bytes: bytes):
    doc = upload(client, sample_contract_bytes).json()
    r = client.get(f"/api/documents/{doc['id']}/file")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == sample_contract_bytes


def test_filename_is_sanitised(client: TestClient):
    r = upload(client, make_text_pdf([["x"]]), "../../etc/passwd\r\nX: y")
    assert r.status_code == 202
    name = r.json()["filename"]
    assert "/" not in name and "\r" not in name and "\n" not in name
    assert name.endswith(".pdf")


def test_rejects_encrypted_pdf(client: TestClient):
    r = upload(client, make_encrypted_pdf())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "encrypted_pdf"
    assert client.get("/api/documents").json()["total"] == 0


def test_rejects_non_pdf_even_with_pdf_content_type(client: TestClient):
    r = upload(client, b"hello, I am a text file", "fake.pdf")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "not_a_pdf"


def test_rejects_oversized_upload(client: TestClient):
    # test settings cap uploads at 2 MB
    big = make_text_pdf([["x"]]) + b"\n%" + b"A" * (2 * 1024 * 1024)
    r = upload(client, big)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "file_too_large"


def test_scanned_pdf_is_marked_unsupported_not_ready(client: TestClient):
    doc = upload(client, make_scanned_pdf(pages=2)).json()
    final = client.get(f"/api/documents/{doc['id']}").json()
    assert final["status"] == "unsupported"
    assert final["extraction"]["unsupported_pages"] == [1, 2]
    pages = client.get(f"/api/documents/{doc['id']}/pages").json()
    assert all(p["kind"] == "scanned" and not p["supported"] for p in pages)


def test_unknown_or_malformed_ids_return_404(client: TestClient):
    assert client.get("/api/documents/not-an-object-id").status_code == 404
    assert client.get("/api/documents/0123456789abcdef01234567").status_code == 404
    assert client.get("/api/documents/0123456789abcdef01234567/pages/1").status_code == 404
    assert client.delete("/api/documents/0123456789abcdef01234567").status_code == 404


def test_other_owner_cannot_see_document(client: TestClient, mongo_client, test_settings):
    """Ownership is enforced in the repository, not just at the route."""
    doc = upload(client, make_text_pdf([["private"]])).json()
    db = mongo_client[test_settings.mongodb_db]
    from bson import ObjectId

    db[DOCUMENTS].update_one({"_id": ObjectId(doc["id"])}, {"$set": {"owner_id": "someone-else"}})
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404
    assert client.get(f"/api/documents/{doc['id']}/file").status_code == 404
    assert client.get("/api/documents").json()["total"] == 0


def test_reprocess_increments_attempts_and_version(client: TestClient):
    doc = upload(client, make_text_pdf([["again"]])).json()
    r = client.post(f"/api/documents/{doc['id']}/reprocess")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["processing"]["attempts"] == 2
    assert body["version"] == 2
    # Pages were replaced, not duplicated.
    assert len(client.get(f"/api/documents/{doc['id']}/pages").json()) == 1


def test_interrupted_processing_is_recovered_on_startup(client: TestClient, mongo_client, test_settings):
    from bson import ObjectId

    from app.main import create_app

    doc = upload(client, make_text_pdf([["crash"]])).json()
    db = mongo_client[test_settings.mongodb_db]
    db[DOCUMENTS].update_one({"_id": ObjectId(doc["id"])}, {"$set": {"status": "processing"}})

    # Simulate a restart: a new app instance runs the recovery step in lifespan.
    with TestClient(create_app(test_settings)) as restarted:
        after = restarted.get(f"/api/documents/{doc['id']}").json()
    assert after["status"] == "error"
    assert "interrupted" in after["processing"]["error"].lower()
    # The explicit retry path brings it back.
    retried = client.post(f"/api/documents/{doc['id']}/reprocess").json()
    assert retried["status"] == "ready"


def test_delete_removes_document_pages_and_file(client: TestClient, mongo_client, test_settings):
    doc = upload(client, make_text_pdf([["a"], ["b"]])).json()
    db = mongo_client[test_settings.mongodb_db]
    assert db[PAGES].count_documents({}) == 2
    assert db[f"{PDF_BUCKET}.files"].count_documents({}) == 1

    r = client.delete(f"/api/documents/{doc['id']}")
    assert r.status_code == 200
    deleted = r.json()["deleted"]
    assert deleted["pages"] == 2 and deleted["files"] == 1 and deleted["documents"] == 1
    assert deleted["findings"] == 0 and deleted["exports"] == 0
    assert db[DOCUMENTS].count_documents({}) == 0
    assert db[PAGES].count_documents({}) == 0
    assert db[f"{PDF_BUCKET}.files"].count_documents({}) == 0
    assert db[f"{PDF_BUCKET}.chunks"].count_documents({}) == 0
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404


def test_history_persists_across_app_restart(client: TestClient, test_settings):
    """The document and its pages must come from MongoDB, not process memory."""
    from app.main import create_app

    doc = upload(client, make_text_pdf([["persisted"]])).json()
    with TestClient(create_app(test_settings)) as restarted:
        listed = restarted.get("/api/documents").json()
        assert [d["id"] for d in listed["items"]] == [doc["id"]]
        page = restarted.get(f"/api/documents/{doc['id']}/pages/1").json()
        assert page["text"] == "persisted"


def test_small_image_page_is_ready_and_reported(client: TestClient):
    doc = upload(client, make_small_image_pdf()).json()
    record = client.get(f"/api/documents/{doc['id']}").json()
    assert record["status"] == "ready"  # still analysed, not refused
    assert record["extraction"]["pages_with_images"] == [1]
    assert any("contain images" in line for line in record["extraction"]["limitations"])
    summary = client.get(f"/api/documents/{doc['id']}/review-summary").json()
    assert any("Pages 1 contain images" in w for w in summary["warnings"])
    assert summary["unsupported_pages"] == []  # no acknowledgement needed for a small image


def test_extraction_over_its_time_budget_becomes_a_retryable_error(client: TestClient, monkeypatch):
    from app.services import document_service

    real = document_service.extract_document
    monkeypatch.setattr(
        document_service, "extract_document", lambda data, time_limit_seconds=None: real(data, time_limit_seconds=0)
    )
    doc = upload(client, make_text_pdf([["a"], ["b"]])).json()
    record = client.get(f"/api/documents/{doc['id']}").json()
    assert record["status"] == "error"
    assert "Extraction stopped" in record["processing"]["error"]

    monkeypatch.setattr(document_service, "extract_document", real)  # the file is fine; retry with the normal budget
    retried = client.post(f"/api/documents/{doc['id']}/reprocess").json()
    assert retried["status"] == "ready" and retried["processing"]["attempts"] == 2
