"""An export authorization is issued FOR someone: the reviewer's own export, or the assistant.

Before this rule the assistant could use any authorization issued in the last five minutes, including
the one the Export tab creates for the reviewer's own click (decision 20's known limit). These tests
pin the closed window: the two kinds are not interchangeable in either direction, and a refused use
consumes nothing.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import EXPORT_AUTHORIZATIONS
from app.models.common import utcnow
from tests.test_export_api import approve_all_emails, authorize, do_export, new_doc
from tests.test_mcp_protocol import call_once


def _export_tool(settings: Settings, doc_id: str):
    return call_once(settings, "export_approved_redactions", {"document_id": doc_id})


def test_an_authorization_for_the_reviewers_own_export_cannot_be_used_by_the_assistant(
    client: TestClient, test_settings: Settings
):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    token = authorize(client, doc_id)  # what the Export tab does on the reviewer's own click

    result = _export_tool(test_settings, doc_id)
    assert not result.ok and "authorization_required" in result.error
    assert client.get(f"/api/documents/{doc_id}/exports").json() == []

    # Nothing was consumed: the reviewer's own export still works with that token.
    assert do_export(client, doc_id, token).status_code == 201


def test_an_authorization_for_the_assistant_cannot_be_used_over_rest(client: TestClient, test_settings: Settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    token = authorize(client, doc_id, audience="assistant")

    refused = do_export(client, doc_id, token)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "authorization_wrong_audience"
    assert client.get(f"/api/documents/{doc_id}/exports").json() == []

    # Still unused: the assistant can export with it.
    assert _export_tool(test_settings, doc_id).ok
    assert len(client.get(f"/api/documents/{doc_id}/exports").json()) == 1


def test_the_assistant_uses_the_newest_assistant_authorization_even_if_a_reviewer_one_is_newer(
    client: TestClient, test_settings: Settings
):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    authorize(client, doc_id, audience="assistant")
    authorize(client, doc_id)  # newer, but for the reviewer

    assert _export_tool(test_settings, doc_id).ok


def test_the_audience_is_chosen_by_the_request_and_defaults_to_the_reviewer(client: TestClient):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    url = f"/api/documents/{doc_id}/export-authorizations"
    assert client.post(url).json()["audience"] == "reviewer"
    assert client.post(url, json={}).json()["audience"] == "reviewer"
    assert client.post(url, json={"audience": "assistant"}).json()["audience"] == "assistant"
    assert client.post(url, json={"audience": "everyone"}).status_code == 422


def test_an_authorization_without_an_audience_counts_as_the_reviewers(
    client: TestClient, mongo_client, test_settings: Settings
):
    """Records written before this rule have no ``audience``; they must not become assistant-usable."""
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    token = authorize(client, doc_id, audience="assistant")
    mongo_client[test_settings.mongodb_db][EXPORT_AUTHORIZATIONS].update_many({}, {"$unset": {"audience": ""}})

    assert "authorization_required" in _export_tool(test_settings, doc_id).error
    assert do_export(client, doc_id, token).status_code == 201  # as a reviewer authorization it works over REST


def test_the_audit_log_records_who_an_authorization_was_for(client: TestClient, mongo_client, test_settings: Settings):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    authorize(client, doc_id)
    authorize(client, doc_id, audience="assistant")
    events = mongo_client[test_settings.mongodb_db]["audit_events"].find({"event": "export_authorized"}).sort("created_at", 1)
    assert [e["details"]["audience"] for e in events] == ["reviewer", "assistant"]


def test_an_assistant_authorization_still_expires_after_five_minutes(
    client: TestClient, mongo_client, test_settings: Settings
):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    authorize(client, doc_id, audience="assistant")
    mongo_client[test_settings.mongodb_db][EXPORT_AUTHORIZATIONS].update_many(
        {}, {"$set": {"created_at": utcnow() - timedelta(minutes=30), "expires_at": utcnow() - timedelta(minutes=25)}}
    )
    assert "authorization_required" in _export_tool(test_settings, doc_id).error
