"""Search endpoint tests against the real database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import make_text_pdf, upload


def test_search_endpoint_returns_occurrences_with_boxes(client: TestClient, sample_contract_bytes: bytes):
    doc = upload(client, sample_contract_bytes).json()
    r = client.get(f"/api/documents/{doc['id']}/search", params={"q": "Maria Delgado-Reyes"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5
    assert body["searched_pages"] == [1, 2, 3] and body["unsupported_pages"] == []
    first = body["occurrences"][0]
    assert first["occurrence_id"].startswith("occ_")
    assert first["mappable"] is True
    assert set(first["boxes"][0]) == {"x0", "y0", "x1", "y1"}


def test_search_options_are_passed_through(client: TestClient, sample_contract_bytes: bytes):
    doc = upload(client, sample_contract_bytes).json()
    url = f"/api/documents/{doc['id']}/search"
    assert client.get(url, params={"q": "maria", "case_sensitive": "true"}).json()["total"] == 1
    assert client.get(url, params={"q": "Mari", "whole_word": "true"}).json()["total"] == 0


def test_search_validation_errors(client: TestClient):
    doc = upload(client, make_text_pdf([["hello"]])).json()
    url = f"/api/documents/{doc['id']}/search"
    assert client.get(url).status_code == 422  # q is required
    r = client.get(url, params={"q": "   "})
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_query"
    r = client.get(url, params={"q": "x" * 201})
    assert r.status_code == 422


def test_search_unknown_or_foreign_document_is_404(client: TestClient):
    assert client.get("/api/documents/0123456789abcdef01234567/search", params={"q": "a"}).status_code == 404
    assert client.get("/api/documents/not-an-id/search", params={"q": "a"}).status_code == 404
