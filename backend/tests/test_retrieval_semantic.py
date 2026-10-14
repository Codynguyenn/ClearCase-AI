"""Retrieval quality with the REAL local embedding model.

Skipped (with a stated reason) when the model is not downloaded; CI downloads it.
Questions are paraphrases of the policies, not copies, and were chosen to sit clearly
on one side of the cutoff so the tests are not brittle. These are checks that the
pipeline works end to end, not a measurement of accuracy: that is
``evaluation/tune_retrieval.py`` (dev set) and the Phase 6 held-out evaluation.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import POLICY_CHUNKS
from app.services.embeddings import get_embedder
from tests.conftest import requires_embedding_model

pytestmark = requires_embedding_model


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    r = client.post("/api/policies/seed-samples", json={})
    assert r.status_code == 200, r.text
    assert all(p["embedding"]["state"] == "ready" for p in r.json())
    return client


def ask(client: TestClient, question: str, top_k: int = 3) -> dict:
    return client.post("/api/policies/search", json={"query": question, "top_k": top_k}).json()


@pytest.mark.parametrize(
    ("question", "policy_id", "section"),
    [
        ("When is it acceptable to redact an organisation name?", "northwind-redaction-policy", "6. Organisation names"),
        ("A nine digit number looks like a tax ID but I am not sure it is real", "northwind-redaction-policy", "4. Government and tax identifiers"),
        ("Does this policy say anything about medical records?", "northwind-redaction-policy", "9. Topics this policy does not address"),
        ("Why does hidden metadata matter when redacting?", "northwind-sharing-guidelines", "4. Hidden content and file metadata"),
        ("How long do we keep the original and the redacted version?", "northwind-sharing-guidelines", "6. Keeping records"),
    ],
)
def test_paraphrased_questions_find_the_right_passage_first(seeded: TestClient, question, policy_id, section):
    result = ask(seeded, question)
    assert result["status"] == "ok", result
    top = result["passages"][0]
    assert (top["policy_id"], top["section"]) == (policy_id, section)
    assert top["score"] >= result["method"]["min_score"]
    assert top["is_fictional_example"] is True
    # The citation shown to the user resolves to exactly the passage that was retrieved.
    shown = seeded.get(f"/api/policies/passages/{top['citation_id']}").json()
    assert shown["text"] == top["text"] and shown["section"] == section


@pytest.mark.parametrize(
    "question",
    [
        "What is the capital of France?",
        "How do I reset my email password?",
        "What are the cafeteria opening hours?",
        "Which font should we use in contracts?",
        "How do I convert a PDF into a Word document?",
    ],
)
def test_questions_the_policies_do_not_address_get_no_relevant_policy(seeded: TestClient, question):
    result = ask(seeded, question)
    assert result["status"] == "no_relevant_policy" and result["passages"] == []
    assert result["top_score_below_cutoff"] < result["method"]["min_score"]
    assert "do not assume a rule" in result["message"].lower()


def test_retrieval_searches_across_all_active_policies(seeded: TestClient):
    result = ask(seeded, "Why does hidden metadata matter when redacting?", top_k=5)
    assert result["chunks_considered"] == 19  # 10 + 9 chunks from both policies
    assert {p["policy_id"] for p in result["passages"]} <= {"northwind-redaction-policy", "northwind-sharing-guidelines"}


def test_stored_vectors_record_the_exact_model_that_made_them(seeded: TestClient, mongo_client, test_settings: Settings):
    chunk = mongo_client[test_settings.mongodb_db][POLICY_CHUNKS].find_one({"citation_id": "northwind-redaction-policy:1.0:3"})
    assert chunk["embedding_model"] == test_settings.embedding_model
    assert chunk["embedding_revision"] == test_settings.embedding_revision
    assert len(chunk["embedding"]) == 384
    assert math.isclose(sum(x * x for x in chunk["embedding"]), 1.0, rel_tol=1e-4)  # unit length: cosine = dot product


def test_the_real_embedder_loads_offline_and_is_pinned(test_settings: Settings):
    embedder = get_embedder(test_settings)
    assert (embedder.model_id, embedder.revision) == (test_settings.embedding_model, test_settings.embedding_revision)
    vectors = embedder.embed(["Client names must be redacted.", "Client names must be redacted."])
    assert embedder.dimension == 384 and embedder.max_tokens == 256
    assert vectors[0] == vectors[1]  # deterministic on CPU
    assert 0 < embedder.count_tokens("Client names must be redacted.") < 20


def test_health_says_semantic_search_is_available(seeded: TestClient):
    emb = seeded.get("/api/health").json()["embeddings"]
    assert emb["available"] is True and emb["dimension"] == 384
