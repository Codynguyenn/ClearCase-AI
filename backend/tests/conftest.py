"""Shared pytest fixtures.

The API tests run against a *real* MongoDB (a separate ``clearcase_test``
database that is dropped before the session). We deliberately do not use an
in-memory fake: GridFS, indexes and query semantics are part of what we are
testing. Start MongoDB with ``scripts/dev_mongo.sh start`` or
``docker compose up mongo`` before running ``pytest``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.config import Settings
from app.main import create_app
from app.services.embeddings import embedding_status
from tests.support.fake_embedder import FakeEmbedder

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_CONTRACT = REPO_ROOT / "sample_data" / "sample_contract.pdf"

TEST_DB = os.environ.get("CLEARCASE_TEST_DB", "clearcase_test")
TEST_URI = os.environ.get("CLEARCASE_MONGODB_URI", "mongodb://127.0.0.1:27017")


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Hermetic: never reads the developer's .env, so a live-mode .env cannot make a test call a paid model."""
    return Settings(
        _env_file=None,
        mongodb_uri=TEST_URI,
        mongodb_db=TEST_DB,
        max_upload_mb=2,
        max_pages=10,
        reindex_on_startup=False,
        assistant_mode="demo",
        llm_provider="",
        llm_api_key="",
        llm_model="",
    )


@pytest.fixture(scope="session")
def mongo_client(test_settings: Settings) -> Iterator[MongoClient]:
    client = MongoClient(test_settings.mongodb_uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        pytest.fail(
            f"MongoDB is required for these tests but is not reachable at {test_settings.mongodb_uri}: {exc}\n"
            "Start it with `scripts/dev_mongo.sh start` or `docker compose up mongo`."
        )
    client.drop_database(test_settings.mongodb_db)
    yield client
    client.close()


@pytest.fixture
def client(test_settings: Settings, mongo_client: MongoClient) -> Iterator[TestClient]:
    """A fresh app + empty database for every test."""
    mongo_client.drop_database(test_settings.mongodb_db)
    app = create_app(test_settings)
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------ policy / embedding fixtures
@pytest.fixture(scope="session")
def fake_settings(test_settings: Settings) -> Settings:
    """Settings for logic tests that use the fake embedder (its own model name/revision, lower cutoff)."""
    return test_settings.model_copy(
        update={"embedding_model": "fake/bow", "embedding_revision": "fake-rev-1", "retrieval_min_score": 0.2}
    )


@pytest.fixture
def fake_client(fake_settings: Settings, mongo_client: MongoClient) -> Iterator[TestClient]:
    """App wired to the fake embedder, on an empty database."""
    mongo_client.drop_database(fake_settings.mongodb_db)
    app = create_app(fake_settings)
    app.state.embedder_provider = lambda: FakeEmbedder(model_id="fake/bow", revision="fake-rev-1")
    with TestClient(app) as c:
        yield c


requires_embedding_model = pytest.mark.skipif(
    not embedding_status(Settings(_env_file=None)).available,
    reason="the real embedding model is not downloaded (run scripts/download_models.sh); CI downloads it",
)


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def sample_contract_bytes() -> bytes:
    assert SAMPLE_CONTRACT.exists(), "run sample_data/generate_samples.py first"
    return SAMPLE_CONTRACT.read_bytes()


def make_text_pdf(lines_per_page: list[list[str]], *, rotation: int = 0) -> bytes:
    doc = pymupdf.open()
    for lines in lines_per_page:
        page = doc.new_page(width=400, height=500)
        y = 50.0
        for line in lines:
            page.insert_text((40, y), line, fontsize=11)
            y += 18
        if rotation:
            page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    return data


def make_scanned_pdf(pages: int = 1) -> bytes:
    """A PDF whose pages are nothing but a full-page image (like a scan)."""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=400, height=500)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 100), False)
        pix.clear_with(200)
        page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def make_mixed_pdf() -> bytes:
    """Text at the top, a large image below (e.g. a scanned exhibit pasted in)."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((40, 40), "Exhibit B attached below", fontsize=11)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 100), False)
    pix.clear_with(120)
    page.insert_image(pymupdf.Rect(20, 80, 380, 480), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def make_small_image_pdf(*, text: str = "Signed by the parties below") -> bytes:
    """A text page with a small image (a logo/stamp, ~2% of the page) that text extraction cannot read."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((40, 40), text, fontsize=11)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
    pix.clear_with(90)
    page.insert_image(pymupdf.Rect(40, 400, 140, 440), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 40), "secret", fontsize=11)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user", owner_pw="owner")
    doc.close()
    return data


def upload(client: TestClient, data: bytes, filename: str = "test.pdf"):
    return client.post("/api/documents", files={"file": (filename, data, "application/pdf")})
