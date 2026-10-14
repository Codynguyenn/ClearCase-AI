"""Policy ingestion, versioning, embedding state, ownership, citations and retrieval *logic*.

These tests use a deterministic fake embedder (``tests/support/fake_embedder.py``), so
they are fast and need no model. They check behaviour: what gets stored, what is
filtered, what is reported honestly. Retrieval *quality* is tested with the real
model in ``test_retrieval_semantic.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.config import Settings
from app.db import POLICIES, POLICY_CHUNKS, Mongo
from app.services.embeddings import EmbeddingUnavailable
from app.services.policy_service import PolicyService
from tests.support.fake_embedder import FakeEmbedder

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data" / "policies"

POLICY_V1 = """---
policy_id: acme-policy
title: Acme Redaction Policy
version: "1.0"
status: FICTIONAL EXAMPLE
---
# Acme Redaction Policy

## 1. Personal names

Client names must be redacted before external sharing.

## 2. Company names

Company names remain visible unless a reviewer asks otherwise.
"""
POLICY_V2 = POLICY_V1.replace('version: "1.0"', 'version: "2.0"').replace(
    "remain visible unless a reviewer asks otherwise", "must always be redacted in this version"
)


def upload(client: TestClient, text: str | bytes, filename: str = "policy.md", **form: str):
    data = text.encode() if isinstance(text, str) else text
    return client.post("/api/policies", files={"file": (filename, data, "text/markdown")}, data=form)


def chunk_docs(mongo_client, settings: Settings, **query) -> list[dict]:
    return list(mongo_client[settings.mongodb_db][POLICY_CHUNKS].find(query))


# ------------------------------------------------------------------------ ingest
def test_upload_chunks_embeds_and_activates_a_policy(fake_client: TestClient):
    r = upload(fake_client, POLICY_V1)
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["policy_id"], p["version"], p["status"]) == ("acme-policy", "1.0", "active")
    assert p["is_fictional_example"] is True and p["chunk_count"] == 2
    assert p["embedding"]["state"] == "ready" and p["embedding"]["chunks_current"] == 2
    assert p["embedding"]["model"] == "fake/bow" and p["embedding"]["revision"] == "fake-rev-1"
    assert [x["policy_id"] for x in fake_client.get("/api/policies").json()] == ["acme-policy"]


def test_chunks_store_ids_section_source_and_embedding_metadata(fake_client: TestClient, mongo_client, fake_settings):
    upload(fake_client, POLICY_V1, filename="acme.md")
    docs = chunk_docs(mongo_client, fake_settings, policy_id="acme-policy")
    assert sorted(d["citation_id"] for d in docs) == ["acme-policy:1.0:1", "acme-policy:1.0:2"]
    d = next(d for d in docs if d["ordinal"] == 1)
    assert d["section"] == "1. Personal names" and d["owner_id"] == "demo-user"
    assert len(d["embedding"]) == 64 and d["embedding_model"] == "fake/bow" and d["embedding_revision"] == "fake-rev-1"
    assert d["embedded_at"] is not None


def test_uploading_the_same_file_again_changes_nothing(fake_client: TestClient, mongo_client, fake_settings):
    first = upload(fake_client, POLICY_V1).json()
    second = upload(fake_client, POLICY_V1)
    assert second.status_code == 201 and second.json()["id"] == first["id"]
    assert len(chunk_docs(mongo_client, fake_settings)) == 2  # no duplicate chunks


def test_same_version_with_different_content_is_refused(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    r = upload(fake_client, POLICY_V1.replace("must be redacted", "may be redacted"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "policy_version_exists"


def test_a_new_version_supersedes_the_old_one_and_the_old_one_stays_readable(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    r = upload(fake_client, POLICY_V2)
    assert r.json()["status"] == "active"
    by_version = {p["version"]: p["status"] for p in fake_client.get("/api/policies").json()}
    assert by_version == {"1.0": "archived", "2.0": "active"}
    old = fake_client.get("/api/policies/passages/acme-policy:1.0:2")
    assert old.status_code == 200 and old.json()["policy_status"] == "archived"  # old citations still resolve


def test_a_version_can_be_reactivated_to_roll_back(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    upload(fake_client, POLICY_V2)
    r = fake_client.post("/api/policies/acme-policy/versions/1.0/activate")
    assert r.status_code == 200 and r.json()["status"] == "active"
    statuses = {p["version"]: p["status"] for p in fake_client.get("/api/policies").json()}
    assert statuses == {"1.0": "active", "2.0": "archived"}


def test_the_database_itself_refuses_two_active_versions(fake_client: TestClient, mongo_client, fake_settings):
    upload(fake_client, POLICY_V1)
    upload(fake_client, POLICY_V2)
    col = mongo_client[fake_settings.mongodb_db][POLICIES]
    with pytest.raises(DuplicateKeyError):
        col.update_one({"version": "1.0"}, {"$set": {"status": "active"}})  # 2.0 is already active


@pytest.mark.parametrize(
    ("filename", "content", "code"),
    [
        ("policy.pdf", b"%PDF-1.4", "unsupported_policy_type"),
        ("policy.md", b"has a \x00 null byte", "invalid_policy"),
        ("policy.md", "caf\xe9".encode("latin-1"), "invalid_policy"),
        ("policy.md", b"   \n\n  ", "empty_policy"),
        ("policy.md", b"# Title only\n", "empty_policy"),
    ],
)
def test_bad_files_are_rejected_with_a_stable_code(fake_client: TestClient, filename, content, code):
    r = upload(fake_client, content, filename=filename)
    assert r.status_code in (413, 422) and r.json()["error"]["code"] == code


def test_oversized_policy_is_rejected(fake_client: TestClient, fake_settings):
    r = upload(fake_client, "## S\n\n" + "word " * (fake_settings.max_policy_kb * 1024 // 5 + 100))
    assert r.status_code == 413 and r.json()["error"]["code"] == "policy_too_large"


@pytest.mark.parametrize(
    ("form", "code"),
    [({"policy_id": "Bad Id!"}, "invalid_policy_id"), ({"version": "1 0"}, "invalid_version"), ({"title": "x" * 201}, None)],
)
def test_invalid_identifiers_are_rejected(fake_client: TestClient, form, code):
    r = upload(fake_client, "## S\n\ntext here.", **form)
    assert r.status_code == 422
    if code:
        assert r.json()["error"]["code"] == code


def test_uploaded_filename_cannot_escape_or_smuggle_paths(fake_client: TestClient):
    r = upload(fake_client, "## S\n\nplain text here.", filename="../../etc/passwd.md")
    assert r.status_code == 201 and r.json()["source"] == "passwd.md" and r.json()["policy_id"] == "passwd"


def test_policy_without_front_matter_gets_sensible_defaults(fake_client: TestClient):
    p = upload(fake_client, "# My Rules\n\n## A\n\nBe careful with names.", filename="my rules.md").json()
    assert p["policy_id"] == "my-rules" and p["version"] == "1" and p["title"] == "My Rules"
    assert p["is_fictional_example"] is False  # only labelled fictional when the file says so


# ---------------------------------------------------------------- embedding state
def test_policy_is_saved_but_marked_unavailable_when_embeddings_cannot_run(
    fake_settings: Settings, mongo_client, fake_client: TestClient
):
    def broken():
        raise EmbeddingUnavailable("The embedding model has not been downloaded.")

    fake_client.app.state.embedder_provider = broken
    p = upload(fake_client, POLICY_V1).json()
    assert p["status"] == "active" and p["chunk_count"] == 2  # nothing lost
    assert p["embedding"]["state"] == "unavailable" and "not been downloaded" in p["embedding"]["detail"]
    result = fake_client.post("/api/policies/search", json={"query": "client names"}).json()
    assert result["status"] == "unavailable" and result["passages"] == []
    assert "not used as a substitute" in result["message"] or "unavailable" in result["message"].lower()

    fake_client.app.state.embedder_provider = lambda: FakeEmbedder()
    fixed = fake_client.post("/api/policies/acme-policy/versions/1.0/reindex").json()
    assert fixed["embedding"]["state"] == "ready"
    assert fake_client.post("/api/policies/search", json={"query": "client names"}).json()["status"] == "ok"


def test_vectors_from_another_model_revision_are_ignored_and_reported(
    fake_client: TestClient, mongo_client, fake_settings
):
    upload(fake_client, POLICY_V1)
    col = mongo_client[fake_settings.mongodb_db][POLICY_CHUNKS]
    col.update_one({"citation_id": "acme-policy:1.0:2"}, {"$set": {"embedding_revision": "older-model-rev"}})
    listed = fake_client.get("/api/policies").json()[0]["embedding"]
    assert listed["state"] == "partial" and listed["chunks_stale"] == 1 and listed["chunks_current"] == 1

    result = fake_client.post("/api/policies/search", json={"query": "client names redacted", "top_k": 5}).json()
    assert result["chunks_stale_ignored"] == 1 and result["chunks_considered"] == 1
    assert "acme-policy:1.0:2" not in [p["citation_id"] for p in result["passages"]]

    fake_client.post("/api/policies/reindex")  # rebuilds only what is stale
    assert fake_client.get("/api/policies").json()[0]["embedding"]["state"] == "ready"


def test_chunks_longer_than_the_model_reads_are_flagged(fake_client: TestClient, fake_settings):
    fake_client.app.state.embedder_provider = lambda: FakeEmbedder(model_id="fake/bow", revision="fake-rev-1", max_tokens=5)
    p = upload(fake_client, "## S\n\nThis passage clearly has more than five words in it.").json()
    assert p["truncated_chunks"] >= 1


# --------------------------------------------------------------------- retrieval
def test_only_active_versions_are_searched(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    upload(fake_client, POLICY_V2)
    result = fake_client.post("/api/policies/search", json={"query": "company names redacted", "top_k": 5}).json()
    assert result["status"] == "ok"
    assert {p["policy_version"] for p in result["passages"]} == {"2.0"}
    assert result["chunks_considered"] == 2  # only the active version's chunks were ever candidates
    fake_client.post("/api/policies/acme-policy/versions/1.0/activate")
    result = fake_client.post("/api/policies/search", json={"query": "company names redacted", "top_k": 5}).json()
    assert {p["policy_version"] for p in result["passages"]} == {"1.0"}


def test_search_with_no_policies_says_so(fake_client: TestClient):
    r = fake_client.post("/api/policies/search", json={"query": "anything at all"}).json()
    assert r["status"] == "no_policies" and r["passages"] == []


def test_an_unrelated_question_gets_no_relevant_policy_not_the_least_bad_passage(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    r = fake_client.post("/api/policies/search", json={"query": "cafeteria lunch hours"}).json()
    assert r["status"] == "no_relevant_policy" and r["passages"] == []
    assert r["top_score_below_cutoff"] is not None
    assert "do not assume a rule" in r["message"].lower()


def test_results_carry_the_method_and_cutoff_and_a_clear_fictional_label(fake_client: TestClient, fake_settings):
    upload(fake_client, POLICY_V1)
    r = fake_client.post("/api/policies/search", json={"query": "client names must be redacted"}).json()
    assert r["status"] == "ok"
    assert r["method"]["kind"] == "semantic_cosine" and r["method"]["min_score"] == fake_settings.retrieval_min_score
    top = r["passages"][0]
    assert top["is_fictional_example"] is True and top["rank"] == 1 and 0 < top["score"] <= 1.0001
    assert top["citation_id"].startswith("acme-policy:1.0:")


def test_search_validates_input(fake_client: TestClient):
    assert fake_client.post("/api/policies/search", json={"query": ""}).status_code == 422
    assert fake_client.post("/api/policies/search", json={"query": "x" * 501}).status_code == 422
    assert fake_client.post("/api/policies/search", json={"query": "ok", "top_k": 99}).status_code == 422


# ------------------------------------------------------------------- ownership
def seed_other_owner(settings: Settings) -> None:
    mongo = Mongo(settings)
    try:
        PolicyService(mongo, settings, lambda: FakeEmbedder()).ingest(
            "someone-else", filename="secret.md", data=POLICY_V1.replace("acme-policy", "their-policy").encode()
        )
    finally:
        mongo.close()


def test_another_owners_policies_are_invisible_to_list_search_and_citations(fake_client: TestClient, fake_settings):
    seed_other_owner(fake_settings)
    assert fake_client.get("/api/policies").json() == []
    assert fake_client.post("/api/policies/search", json={"query": "client names"}).json()["status"] == "no_policies"
    assert fake_client.get("/api/policies/passages/their-policy:1.0:2").status_code == 404
    r = fake_client.post("/api/policies/passages/resolve", json={"citation_ids": ["their-policy:1.0:2"]}).json()
    assert r["found"] == [] and r["missing"] == ["their-policy:1.0:2"]
    assert fake_client.delete("/api/policies/their-policy").status_code == 404
    assert fake_client.post("/api/policies/their-policy/versions/1.0/activate").status_code == 404


def test_two_owners_with_the_same_policy_id_and_version_never_see_each_others_chunks(
    fake_client: TestClient, fake_settings: Settings
):
    """The owner filter must be in the chunk query itself: policy_id:version alone is not unique across owners."""
    upload(fake_client, POLICY_V1)
    mongo = Mongo(fake_settings)
    try:
        theirs = POLICY_V1.replace("Client names must be redacted", "THEIR PRIVATE RULE about client names")
        PolicyService(mongo, fake_settings, lambda: FakeEmbedder()).ingest(
            "someone-else", filename="acme.md", data=theirs.encode()
        )
    finally:
        mongo.close()
    result = fake_client.post("/api/policies/search", json={"query": "client names redacted", "top_k": 5}).json()
    assert result["status"] == "ok"
    assert all("THEIR PRIVATE RULE" not in p["text"] for p in result["passages"])
    assert result["chunks_considered"] == 2  # my two chunks, not four
    passage = fake_client.get("/api/policies/passages/acme-policy:1.0:1").json()
    assert "THEIR PRIVATE RULE" not in passage["text"]


# -------------------------------------------------------------------- citations
def test_resolving_citations_separates_real_from_invalid(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    ids = ["acme-policy:1.0:2", "acme-policy:1.0:99", "nope:1:1", "not a citation", "../../etc/passwd", "acme-policy:1.0:2"]
    r = fake_client.post("/api/policies/passages/resolve", json={"citation_ids": ids}).json()
    assert [p["citation_id"] for p in r["found"]] == ["acme-policy:1.0:2"]
    assert set(r["missing"]) == {"acme-policy:1.0:99", "nope:1:1", "not a citation", "../../etc/passwd"}


def test_a_passage_can_be_read_and_matches_the_stored_text(fake_client: TestClient):
    upload(fake_client, POLICY_V1)
    p = fake_client.get("/api/policies/passages/acme-policy:1.0:1").json()
    assert p["text"] == "Client names must be redacted before external sharing."
    assert p["section"] == "1. Personal names" and p["policy_title"] == "Acme Redaction Policy"
    assert p["is_fictional_example"] is True and p["source"] == "policy.md"
    assert fake_client.get("/api/policies/passages/acme-policy:1.0:99").status_code == 404
    assert fake_client.get("/api/policies/passages/bad id").status_code == 404


# ------------------------------------------------------------------- deletion
def test_deleting_a_policy_removes_its_chunks_but_not_other_policies(fake_client: TestClient, mongo_client, fake_settings):
    upload(fake_client, POLICY_V1)
    upload(fake_client, POLICY_V1.replace("acme-policy", "other-policy"))
    r = fake_client.delete("/api/policies/acme-policy")
    assert r.status_code == 200 and r.json() == {"policies": 1, "chunks": 2}
    assert [p["policy_id"] for p in fake_client.get("/api/policies").json()] == ["other-policy"]
    assert len(chunk_docs(mongo_client, fake_settings)) == 2
    assert fake_client.get("/api/policies/passages/acme-policy:1.0:2").status_code == 404


def test_deleting_one_version_keeps_the_others(fake_client: TestClient, mongo_client, fake_settings):
    upload(fake_client, POLICY_V1)
    upload(fake_client, POLICY_V2)
    assert fake_client.delete("/api/policies/acme-policy/versions/1.0").json() == {"policies": 1, "chunks": 2}
    assert [p["version"] for p in fake_client.get("/api/policies").json()] == ["2.0"]


# ------------------------------------------------------------------------ seed
def test_seeding_loads_both_fictional_policies_and_is_repeatable(
    fake_client: TestClient, fake_settings: Settings, mongo_client
):
    fake_client.app.dependency_overrides  # app already uses fake_settings; point sample dir at the repo
    first = fake_client.post("/api/policies/seed-samples", json={}).json()
    assert {p["policy_id"] for p in first} == {"northwind-redaction-policy", "northwind-sharing-guidelines"}
    assert all(p["is_fictional_example"] and p["status"] == "active" for p in first)
    total = len(chunk_docs(mongo_client, fake_settings))
    fake_client.post("/api/policies/seed-samples", json={})
    assert len(chunk_docs(mongo_client, fake_settings)) == total  # repeatable without duplicates
    fake_client.post("/api/policies/seed-samples", json={"reset": True})
    assert len(chunk_docs(mongo_client, fake_settings)) == total  # reset rebuilds the same corpus


def test_health_reports_embedding_status(fake_client: TestClient):
    emb = fake_client.get("/api/health").json()["embeddings"]
    assert {"available", "enabled", "model", "revision"} <= set(emb)
