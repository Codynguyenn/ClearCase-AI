"""At startup, policy passages embedded by a different model revision are quietly refreshed in the background."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.config import Settings
from app.db import POLICIES, POLICY_CHUNKS, Mongo
from app.main import create_app
from app.services.policy_service import PolicyService
from tests.support.fake_embedder import FakeEmbedder

POLICY = b"## 1. Names\n\nClient names must be redacted.\n\n## 2. Dates\n\nDates may stay visible.\n"
REV1, REV2 = "fake-rev-1", "fake-rev-2"


@pytest.fixture
def old_revision_policies(fake_settings: Settings, mongo_client: MongoClient):
    """Two owners each have a policy embedded with revision 1."""
    mongo_client.drop_database(fake_settings.mongodb_db)
    mongo = Mongo(fake_settings)
    try:
        service = PolicyService(mongo, fake_settings, lambda: FakeEmbedder(revision=REV1))
        service.ingest("demo-user", filename="a.md", data=POLICY)
        service.ingest("someone-else", filename="b.md", data=POLICY)
    finally:
        mongo.close()
    return mongo_client[fake_settings.mongodb_db]


def run_app(settings: Settings, embedder: FakeEmbedder | None, *, owner: str = "demo-user"):
    """Start the app (which runs the lifespan), wait for the background refresh, return (thread, health, policies)."""
    app = create_app(settings.model_copy(update={"demo_owner_id": owner}))
    if embedder is not None:
        app.state.embedder_provider = lambda: embedder
    with TestClient(app) as client:
        thread = getattr(app.state, "reindex_thread", None)
        if thread is not None:
            thread.join(timeout=20)
            assert not thread.is_alive()
        return thread, client.get("/api/health").json(), client.get("/api/policies").json()


def revisions(db, owner: str) -> set[str | None]:
    return {c.get("embedding_revision") for c in db[POLICY_CHUNKS].find({"owner_id": owner})}


def test_stale_passages_of_every_owner_are_refreshed_at_startup(fake_settings, old_revision_policies):
    db = old_revision_policies
    new = fake_settings.model_copy(update={"embedding_revision": REV2, "reindex_on_startup": True})
    assert revisions(db, "demo-user") == {REV1}

    _, health, policies = run_app(new, FakeEmbedder(revision=REV2))

    assert health["status"] == "ok"  # startup was not blocked or broken
    assert revisions(db, "demo-user") == {REV2} and revisions(db, "someone-else") == {REV2}
    assert policies[0]["embedding"]["state"] == "ready"


def test_nothing_happens_when_the_setting_is_off(fake_settings, old_revision_policies):
    db = old_revision_policies
    off = fake_settings.model_copy(update={"embedding_revision": REV2, "reindex_on_startup": False})

    thread, _, policies = run_app(off, FakeEmbedder(revision=REV2))

    assert thread is None
    assert revisions(db, "demo-user") == {REV1}
    assert policies[0]["embedding"]["state"] == "stale"  # still reported honestly, and the button still works


def test_up_to_date_passages_are_never_re_embedded(fake_settings, old_revision_policies):
    embedder = FakeEmbedder(revision=REV1)
    same = fake_settings.model_copy(update={"embedding_revision": REV1, "reindex_on_startup": True})

    run_app(same, embedder)

    assert embedder.calls == 0  # the model was not even asked


def test_a_failing_refresh_is_logged_and_does_not_stop_the_app(fake_settings, old_revision_policies, caplog):
    new = fake_settings.model_copy(update={"embedding_revision": REV2, "reindex_on_startup": True})

    class Exploding(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("model blew up")

    with caplog.at_level(logging.ERROR):
        _, health, _ = run_app(new, Exploding(revision=REV2))

    assert health["status"] == "ok"
    assert any("Startup re-index failed" in r.message for r in caplog.records)
    assert revisions(old_revision_policies, "demo-user") == {REV1}


def test_it_is_skipped_when_no_embedding_model_is_available(fake_settings, old_revision_policies):
    # No injected embedder and the "fake/bow" model is not on disk, so nothing could succeed: leave policies alone.
    before = {p["_id"]: p["updated_at"] for p in old_revision_policies[POLICIES].find({})}
    new = fake_settings.model_copy(update={"embedding_revision": REV2, "reindex_on_startup": True})

    thread, health, _ = run_app(new, None)

    assert thread is None and health["status"] == "ok"
    assert {p["_id"]: p["updated_at"] for p in old_revision_policies[POLICIES].find({})} == before
