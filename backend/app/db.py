"""MongoDB connection management.

Design notes (plain English):

* We use the official synchronous ``pymongo`` driver. FastAPI runs synchronous
  endpoint functions in a thread pool, so this stays simple and fast enough for
  a local, single-user MVP without introducing a second async driver.
* One ``MongoClient`` is created per process and shared. The driver manages its
  own connection pool internally; creating a client per request would be wrong.
* GridFS is MongoDB's standard way to store files larger than the 16 MB document
  limit. It splits a file into chunks and keeps metadata in ``fs.files``. We use
  it for original and exported PDFs and reference the file ids from our own
  ``documents`` records, so metadata records stay small.
* Indexes are declared in one place (``ensure_indexes``) and created at startup.
  MongoDB's ``create_index`` is idempotent, so running it on every boot is safe.
"""

from __future__ import annotations

import logging

from gridfs import GridFSBucket
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.config import Settings

logger = logging.getLogger(__name__)

# Collection names live here so repositories and tests agree on them.
DOCUMENTS = "documents"
PAGES = "pages"
# Collections for later phases are listed now so the deletion cascade and
# index setup have one obvious place to grow.
FINDINGS = "findings"
CONVERSATIONS = "conversations"
MESSAGES = "messages"
POLICIES = "policies"
POLICY_CHUNKS = "policy_chunks"
AGENT_RUNS = "agent_runs"
TOOL_EVENTS = "tool_events"
EXPORTS = "exports"
EXPORT_AUTHORIZATIONS = "export_authorizations"
REVIEW_REVISIONS = "review_revisions"
AUDIT_EVENTS = "audit_events"

PDF_BUCKET = "pdf_files"


class Mongo:
    """Holds the shared client/database for the application lifetime."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: MongoClient = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=5000,
            appname="clearcase-backend",
        )
        self.db: Database = self.client[settings.mongodb_db]
        self.pdf_bucket = GridFSBucket(self.db, bucket_name=PDF_BUCKET)

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except PyMongoError as exc:  # pragma: no cover - exercised only when DB is down
            logger.warning("MongoDB ping failed: %s", exc)
            return False

    def ensure_indexes(self) -> None:
        """Create the indexes the application actually queries on."""
        db = self.db
        # Document list for an owner, newest first; and hash lookup for duplicates.
        db[DOCUMENTS].create_index([("owner_id", ASCENDING), ("created_at", DESCENDING)])
        db[DOCUMENTS].create_index([("owner_id", ASCENDING), ("content_hash", ASCENDING)])
        # Pages are fetched by (document, page number); unique prevents double extraction.
        db[PAGES].create_index(
            [("document_id", ASCENDING), ("page_number", ASCENDING)], unique=True
        )
        # Findings: the review screen filters by status/category within one document.
        db[FINDINGS].create_index([("document_id", ASCENDING), ("owner_id", ASCENDING), ("status", ASCENDING)])
        db[FINDINGS].create_index([("document_id", ASCENDING), ("page_number", ASCENDING)])
        db[FINDINGS].create_index([("document_id", ASCENDING), ("group_key", ASCENDING)])
        db[REVIEW_REVISIONS].create_index([("document_id", ASCENDING), ("revision", DESCENDING)])
        db[EXPORTS].create_index([("document_id", ASCENDING), ("created_at", DESCENDING)])
        # Authorizations expire on their own; code also checks expires_at because
        # MongoDB's TTL sweeper only runs about once a minute.
        db[EXPORT_AUTHORIZATIONS].create_index("expires_at", expireAfterSeconds=0)
        db[EXPORT_AUTHORIZATIONS].create_index([("document_id", ASCENDING)])
        db[AUDIT_EVENTS].create_index([("document_id", ASCENDING), ("created_at", DESCENDING)])

    def close(self) -> None:
        self.client.close()
