"""One click to propose the other copies of text the reviewer approved.

Background: the verifier reports "N other copies of approved text are still readable" when a detector
caught one mention of a name but missed another. Before this feature the reviewer had to search for the
text and propose each copy by hand. The endpoint proposes them (never approves them), so the reviewer
still decides, and the export becomes clean once they approve and export again.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import make_text_pdf, upload
from tests.test_export_api import approve_all_emails, authorize, do_export, new_doc, pdf_text

URL = "/api/documents/{doc}/findings/propose-remaining-copies"


def approve_first_alice(client: TestClient, doc_id: str) -> dict:
    """Approve only the first of two "Alice Smith" mentions, so one readable copy remains."""
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    created = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [occ[0]["occurrence_id"]], "category": "PERSON"},
    ).json()["created"]
    assert client.post(f"/api/documents/{doc_id}/findings/{created[0]['id']}/decision", json={"action": "approve"}).status_code == 200
    return created[0]


def summary(client: TestClient, doc_id: str) -> dict:
    return client.get(f"/api/documents/{doc_id}/review-summary").json()


def revision(client: TestClient, doc_id: str) -> int:
    return summary(client, doc_id)["review_revision"]


def propose(client: TestClient, doc_id: str):
    return client.post(URL.format(doc=doc_id))


def test_it_proposes_the_uncovered_copy_in_the_same_category_and_approves_nothing(client: TestClient):
    doc_id = new_doc(client)
    first = approve_first_alice(client, doc_id)
    assert summary(client, doc_id)["unproposed_copies"] == 1

    r = propose(client, doc_id)
    assert r.status_code == 201, r.text
    created = r.json()["created"]
    assert len(created) == 1
    copy = created[0]
    assert copy["text"] == "Alice Smith" and copy["category"] == first["category"]
    assert copy["status"] == "proposed" and copy["boxes"], "a proposal the reviewer can see and approve"
    assert copy["id"] != first["id"]
    assert summary(client, doc_id)["approved"] == 1  # nothing was approved on the reviewer's behalf


def test_after_proposing_there_is_nothing_left_uncovered_and_the_review_must_be_finished(client: TestClient):
    doc_id = new_doc(client)
    approve_first_alice(client, doc_id)
    propose(client, doc_id)
    s = summary(client, doc_id)
    assert s["unproposed_copies"] == 0
    assert s["unresolved"] == 1 and s["can_export"] is False  # the new proposal blocks export until it is decided


def test_approving_the_proposed_copy_gives_a_clean_export_with_no_readable_copy(client: TestClient):
    doc_id = new_doc(client)
    first = approve_first_alice(client, doc_id)
    before = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert before["requires_acknowledgement"] is True and before["retained"]["unresolved"] == 1

    created = propose(client, doc_id).json()["created"]
    # The earlier export is now out of date: the review changed.
    assert client.get(f"/api/documents/{doc_id}/exports/{before['id']}").json()["stale"] is True

    client.post(f"/api/documents/{doc_id}/findings/bulk-decision", json={"action": "approve", "finding_ids": [created[0]["id"]]})
    after = do_export(client, doc_id, authorize(client, doc_id)).json()
    assert after["status"] == "verified" and after["has_warnings"] is False
    assert after["requires_acknowledgement"] is False and after["retained"]["unresolved"] == 0
    assert after["approved_count"] == 2 and first["id"] != created[0]["id"]
    data = client.get(f"/api/documents/{doc_id}/exports/{after['id']}/file").content
    assert "Alice" not in pdf_text(data)


def test_calling_it_twice_creates_no_duplicates_and_does_not_move_the_revision(client: TestClient):
    doc_id = new_doc(client)
    approve_first_alice(client, doc_id)
    first = propose(client, doc_id).json()
    rev = revision(client, doc_id)
    second = propose(client, doc_id).json()
    assert len(first["created"]) == 1
    assert second["created"] == [] and any("No other copies" in n for n in second["notes"])
    assert revision(client, doc_id) == rev
    assert len(client.get(f"/api/documents/{doc_id}/findings").json()["items"]) == 2


def test_when_nothing_is_uncovered_it_says_so_and_changes_nothing(client: TestClient):
    doc_id = new_doc(client)
    approve_all_emails(client, doc_id)
    rev = revision(client, doc_id)
    r = propose(client, doc_id)
    assert r.status_code == 201
    body = r.json()
    assert body["created"] == [] and any("No other copies" in n for n in body["notes"])
    assert revision(client, doc_id) == rev


def test_with_no_approved_text_there_is_nothing_to_copy(client: TestClient):
    doc_id = new_doc(client)
    body = propose(client, doc_id).json()
    assert body["created"] == [] and any("No other copies" in n for n in body["notes"])


def test_a_copy_the_reviewer_rejected_is_not_proposed_again(client: TestClient):
    doc_id = new_doc(client)
    approve_first_alice(client, doc_id)
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    second = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [occ[1]["occurrence_id"]], "category": "PERSON"},
    ).json()["created"][0]
    client.post(f"/api/documents/{doc_id}/findings/{second['id']}/decision", json={"action": "reject"})  # kept on purpose

    assert propose(client, doc_id).json()["created"] == []
    assert summary(client, doc_id)["unproposed_copies"] == 0


def test_copies_in_a_different_case_are_found_and_keep_the_pages_own_spelling(client: TestClient):
    doc_id = upload(client, make_text_pdf([["Contact Alice Smith today.", "Later ALICE SMITH again.", "And alice smith."]])).json()["id"]
    occ = client.get(f"/api/documents/{doc_id}/search", params={"q": "Alice Smith"}).json()["occurrences"]
    created = client.post(
        f"/api/documents/{doc_id}/findings/propose-search",
        json={"q": "Alice Smith", "occurrence_ids": [occ[0]["occurrence_id"]], "category": "PERSON", "case_sensitive": False},
    ).json()["created"]
    client.post(f"/api/documents/{doc_id}/findings/{created[0]['id']}/decision", json={"action": "approve"})

    copies = propose(client, doc_id).json()["created"]
    assert sorted(c["text"] for c in copies) == ["ALICE SMITH", "alice smith"]
    assert {c["category"] for c in copies} == {"PERSON"}


def test_the_proposed_copies_belong_to_the_same_group_so_one_decision_can_cover_them_all(client: TestClient):
    doc_id = new_doc(client)
    first = approve_first_alice(client, doc_id)
    copy = propose(client, doc_id).json()["created"][0]
    assert copy["group_key"] == first["group_key"]

    # "apply to all matching" on the approved finding approves the proposed copy as well.
    r = client.post(
        f"/api/documents/{doc_id}/findings/{first['id']}/decision",
        json={"action": "approve", "apply_to_all_matching": True},
    )
    assert r.status_code == 200
    assert summary(client, doc_id)["approved"] == 2


def test_the_copies_are_labelled_as_coming_from_the_reviewers_own_approval(client: TestClient):
    doc_id = new_doc(client)
    approve_first_alice(client, doc_id)
    copy = propose(client, doc_id).json()["created"][0]
    assert copy["source"] == "copy:approved_text" and copy["origin"] == "search" and copy["label"] == "user_specified"
    assert "approved" in copy["explanation"].lower()


def test_an_unknown_document_is_not_found(client: TestClient):
    assert propose(client, "doesnotexist").status_code == 404
