"""The reviewer-only "possibly related" tier.

Passages whose similarity is a little below the cutoff are offered to the HUMAN reviewer when nothing
reaches the cutoff. They must never be given to the assistant. These tests use the fake embedder and
build their cutoffs from the scores actually observed, so they check the tier's rules rather than any
particular model's numbers.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.config import Settings
from app.db import Mongo
from app.main import create_app
from app.models.policies import RetrievalStatus
from app.services.policy_service import PolicyService
from app.services.retrieval import WEAK_PASSAGE_LIMIT, MongoCosineRetriever
from tests.support.fake_embedder import FakeEmbedder
from tests.test_policy_api import POLICY_V1

OWNER = "demo-user"
QUERY = "client names redacted"


def with_thresholds(settings: Settings, *, cutoff: float, weak: float) -> Settings:
    return settings.model_copy(update={"retrieval_min_score": cutoff, "retrieval_weak_min_score": weak})


@pytest.fixture
def corpus(fake_settings: Settings, mongo_client: MongoClient):
    """An empty database with one fake-embedded policy, plus a helper to build retrievers."""
    mongo_client.drop_database(fake_settings.mongodb_db)
    mongo = Mongo(fake_settings)
    mongo.ensure_indexes()
    PolicyService(mongo, fake_settings, lambda: FakeEmbedder()).ingest(OWNER, filename="acme.md", data=POLICY_V1.encode())

    def retriever(cutoff: float, weak: float) -> MongoCosineRetriever:
        return MongoCosineRetriever(mongo, with_thresholds(fake_settings, cutoff=cutoff, weak=weak), lambda: FakeEmbedder())

    yield mongo, retriever
    mongo.close()


def best_cosine(retriever) -> float:
    return retriever(-1.0, -1.0).retrieve(OWNER, QUERY, top_k=5).passages[0].score


def test_weak_passages_appear_only_when_nothing_reached_the_cutoff(corpus):
    _mongo, retriever = corpus
    top = best_cosine(retriever)
    result = retriever(top + 0.05, top - 0.05).retrieve(OWNER, QUERY)
    assert result.status == RetrievalStatus.NO_RELEVANT_POLICY and result.passages == []
    assert result.weak_passages, "a passage just below the cutoff should be offered to the reviewer"
    assert all(top - 0.05 <= p.score < top + 0.05 for p in result.weak_passages)
    assert [p.rank for p in result.weak_passages] == list(range(1, len(result.weak_passages) + 1))
    assert result.method.weak_min_score == pytest.approx(top - 0.05)


def test_no_weak_passages_when_real_passages_were_found(corpus):
    _mongo, retriever = corpus
    top = best_cosine(retriever)
    result = retriever(top - 0.05, top - 0.2).retrieve(OWNER, QUERY)
    assert result.status == RetrievalStatus.OK and result.passages
    assert result.weak_passages == []  # the weak tier is only for the "does not address this" state


def test_weak_passages_below_the_lower_bound_are_not_offered(corpus):
    _mongo, retriever = corpus
    top = best_cosine(retriever)
    result = retriever(top + 0.05, top + 0.02).retrieve(OWNER, QUERY)  # lower bound above every score
    assert result.status == RetrievalStatus.NO_RELEVANT_POLICY and result.weak_passages == []


def test_the_tier_is_off_when_its_bound_is_not_below_the_cutoff(corpus):
    _mongo, retriever = corpus
    top = best_cosine(retriever)
    for weak in (top + 0.05, top + 0.2):  # equal to, or above, the cutoff
        result = retriever(top + 0.05, weak).retrieve(OWNER, QUERY)
        assert result.weak_passages == [] and result.method.weak_min_score is None


def test_at_most_three_weak_passages_are_offered(fake_settings: Settings, mongo_client: MongoClient):
    mongo_client.drop_database(fake_settings.mongodb_db)
    mongo = Mongo(fake_settings)
    mongo.ensure_indexes()
    try:
        sections = "\n\n".join(f"## {n}. Rule {n}\n\nClient names are discussed in rule number {n}." for n in range(1, 8))
        PolicyService(mongo, fake_settings, lambda: FakeEmbedder()).ingest(
            OWNER, filename="many.md", data=f"# Many rules\n\n{sections}\n".encode()
        )
        retriever = MongoCosineRetriever(mongo, with_thresholds(fake_settings, cutoff=0.999, weak=0.0), lambda: FakeEmbedder())
        result = retriever.retrieve(OWNER, QUERY, top_k=5)
        assert result.status == RetrievalStatus.NO_RELEVANT_POLICY
        assert len(result.weak_passages) == WEAK_PASSAGE_LIMIT == 3
    finally:
        mongo.close()


def test_weak_passages_never_include_another_owners_text(fake_settings: Settings, mongo_client: MongoClient):
    mongo_client.drop_database(fake_settings.mongodb_db)
    mongo = Mongo(fake_settings)
    mongo.ensure_indexes()
    try:
        service = PolicyService(mongo, fake_settings, lambda: FakeEmbedder())
        service.ingest(OWNER, filename="acme.md", data=POLICY_V1.encode())
        service.ingest("someone-else", filename="acme.md", data=POLICY_V1.replace("Client names", "THEIR SECRET RULE on client names").encode())
        retriever = MongoCosineRetriever(mongo, with_thresholds(fake_settings, cutoff=0.999, weak=0.0), lambda: FakeEmbedder())
        result = retriever.retrieve(OWNER, QUERY, top_k=5)
        assert result.weak_passages
        assert all("THEIR SECRET RULE" not in p.text for p in result.weak_passages)
    finally:
        mongo.close()


def test_the_search_endpoint_returns_weak_passages_for_the_reviewer(fake_settings: Settings, mongo_client: MongoClient, corpus):
    _mongo, retriever = corpus
    top = best_cosine(retriever)
    app = create_app(with_thresholds(fake_settings, cutoff=top + 0.05, weak=top - 0.05))
    app.state.embedder_provider = lambda: FakeEmbedder()
    with TestClient(app) as client:
        body = client.post("/api/policies/search", json={"query": QUERY}).json()
    assert body["status"] == "no_relevant_policy" and body["passages"] == []
    assert body["weak_passages"] and body["weak_passages"][0]["citation_id"].startswith("acme-policy:1.0:")
    assert body["method"]["weak_min_score"] == pytest.approx(top - 0.05)
