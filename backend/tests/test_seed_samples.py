"""The sample-data seed script: loads the shipped samples, is safe to repeat, and --reset touches only samples."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.cli import main, seed_samples
from app.config import Settings
from app.db import DOCUMENTS, FINDINGS, PAGES, POLICIES, Mongo
from app.main import create_app
from app.services.document_service import DocumentService
from tests.conftest import SAMPLE_CONTRACT, make_text_pdf, upload
from tests.support.fake_embedder import FakeEmbedder

CONTRACT_HASH = hashlib.sha256(SAMPLE_CONTRACT.read_bytes()).hexdigest()


def fake():
    return FakeEmbedder(model_id="fake/bow", revision="fake-rev-1")


@pytest.fixture
def db(fake_settings: Settings, mongo_client: MongoClient):
    mongo_client.drop_database(fake_settings.mongodb_db)
    return mongo_client[fake_settings.mongodb_db]


def seed(settings: Settings, *, reset: bool = False) -> int:
    return seed_samples(settings, reset=reset, embedder_provider=fake)


def sample_docs(db, owner: str = "demo-user") -> list[dict]:
    return list(db[DOCUMENTS].find({"owner_id": owner, "content_hash": CONTRACT_HASH}))


def test_seeds_the_sample_contract_and_both_fictional_policies(fake_settings, db, capsys):
    assert seed(fake_settings) == 0
    docs = sample_docs(db)
    assert len(docs) == 1 and docs[0]["status"] == "ready" and docs[0]["page_count"] == 3
    assert sorted(p["policy_id"] for p in db[POLICIES].find({})) == [
        "northwind-redaction-policy",
        "northwind-sharing-guidelines",
    ]
    out = capsys.readouterr().out
    assert "sample_contract.pdf" in out and "northwind-redaction-policy" in out and "ready" in out
    assert fake_settings.mongodb_db in out  # says which database it is changing


def test_running_it_twice_does_not_duplicate_anything(fake_settings, db):
    assert seed(fake_settings) == 0
    first_id = sample_docs(db)[0]["_id"]
    chunks = db["policy_chunks"].count_documents({})
    assert seed(fake_settings) == 0
    docs = sample_docs(db)
    assert len(docs) == 1 and docs[0]["_id"] == first_id
    assert db[POLICIES].count_documents({}) == 2 and db["policy_chunks"].count_documents({}) == chunks


def test_reset_rebuilds_only_the_samples_and_removes_what_hung_off_the_old_copy(fake_settings, db, capsys):
    client = TestClient(create_app(fake_settings))
    with client:
        assert seed(fake_settings) == 0
        old = sample_docs(db)[0]
        old_id = str(old["_id"])
        # Review work on the sample copy, a different upload with its own findings, a user policy, and another
        # owner's copy of the very same PDF.
        client.post(f"/api/documents/{old_id}/findings/propose-detected", json={"categories": ["EMAIL"]})
        assert db[FINDINGS].count_documents({"document_id": old_id}) > 0
        mine = upload(client, make_text_pdf([["Contact me at keep.me@example.com"]]), "mine.pdf").json()["id"]
        client.post(f"/api/documents/{mine}/findings/propose-detected", json={"categories": ["EMAIL"]})
        mine_findings = db[FINDINGS].count_documents({"document_id": mine})
        client.post("/api/policies", files={"file": ("acme.md", b"## Scope\n\nAcme rules about shared mailboxes.", "text/markdown")})
        other = Mongo(fake_settings)
        try:
            other_doc = DocumentService(other, fake_settings).upload(
                owner_id="someone-else", filename="sample_contract.pdf", data=SAMPLE_CONTRACT.read_bytes()
            )
        finally:
            other.close()
        capsys.readouterr()

        assert seed(fake_settings, reset=True) == 0

        new = sample_docs(db)
        assert len(new) == 1 and str(new[0]["_id"]) != old_id and new[0]["status"] == "ready"
        # everything that belonged to the old sample copy is gone...
        assert db[FINDINGS].count_documents({"document_id": old_id}) == 0
        assert db[PAGES].count_documents({"document_id": old_id}) == 0
        # ...and nothing else was touched
        assert db[DOCUMENTS].count_documents({"owner_id": "demo-user"}) == 2
        assert db[FINDINGS].count_documents({"document_id": mine}) == mine_findings
        assert db[DOCUMENTS].count_documents({"_id": ObjectId(other_doc.id)}) == 1
        assert "acme" in {p["policy_id"] for p in db[POLICIES].find({})}
    out = capsys.readouterr().out
    assert "removed 1 old sample document" in out


def test_a_renamed_copy_of_the_sample_is_still_recognised_by_its_content(fake_settings, db):
    with TestClient(create_app(fake_settings)) as client:
        upload(client, SAMPLE_CONTRACT.read_bytes(), "my renamed copy.pdf")
        assert seed(fake_settings) == 0
        assert len(sample_docs(db)) == 1  # not loaded a second time
        assert seed(fake_settings, reset=True) == 0
        assert len(sample_docs(db)) == 1


def test_missing_sample_pdfs_is_a_clear_error_not_a_traceback(fake_settings, db, tmp_path: Path, capsys):
    empty = fake_settings.model_copy(update={"sample_data_dir": str(tmp_path)})
    assert seed(empty) == 1
    err = capsys.readouterr().err
    assert "No sample PDFs found" in err and "generate_samples.py" in err
    assert db[POLICIES].count_documents({}) == 0


def test_unreachable_database_is_a_clear_error(fake_settings, capsys):
    unreachable = fake_settings.model_copy(update={"mongodb_uri": "mongodb://127.0.0.1:1"})
    assert seed(unreachable) == 1
    assert "MongoDB is not reachable" in capsys.readouterr().err


def test_reset_flag_is_rejected_for_other_commands():
    with pytest.raises(SystemExit) as exc:
        main(["embedding-status", "--reset"])
    assert exc.value.code == 2
