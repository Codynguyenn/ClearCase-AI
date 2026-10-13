"""Document and page repositories."""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from app.db import DOCUMENTS, PAGES
from app.models.common import utcnow
from app.models.documents import (
    DocumentRecord,
    DocumentStatus,
    PageSummary,
    PageText,
)
from app.services.errors import NotFoundError
from app.services.pdf_processing import ExtractedPage


def to_object_id(value: str) -> ObjectId:
    """Convert a client-supplied id; an unparsable id is simply 'not found'."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as exc:
        raise NotFoundError("Document not found.") from exc


def _record_from_doc(doc: dict[str, Any]) -> DocumentRecord:
    data = dict(doc)
    data["id"] = str(data.pop("_id"))
    data.pop("original_file_id", None)  # internal reference, not exposed
    return DocumentRecord.model_validate(data)


class DocumentRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[DOCUMENTS]

    def insert(
        self,
        *,
        owner_id: str,
        filename: str,
        content_hash: str,
        size_bytes: int,
        page_count: int,
        original_file_id: ObjectId,
    ) -> DocumentRecord:
        now = utcnow()
        doc = {
            "owner_id": owner_id,
            "filename": filename,
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "page_count": page_count,
            "status": DocumentStatus.UPLOADED.value,
            "version": 1,
            "review_revision": 0,
            "extraction": None,
            "processing": {"attempts": 0, "started_at": None, "finished_at": None, "error": None},
            "original_file_id": original_file_id,
            "created_at": now,
            "updated_at": now,
        }
        result = self._col.insert_one(doc)
        doc["_id"] = result.inserted_id
        return _record_from_doc(doc)

    def get_raw(self, document_id: str, *, owner_id: str) -> dict[str, Any]:
        doc = self._col.find_one({"_id": to_object_id(document_id), "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Document not found.")
        return doc

    def get(self, document_id: str, *, owner_id: str) -> DocumentRecord:
        return _record_from_doc(self.get_raw(document_id, owner_id=owner_id))

    def list(self, *, owner_id: str, limit: int = 100) -> list[DocumentRecord]:
        cursor = self._col.find({"owner_id": owner_id}).sort("created_at", DESCENDING).limit(limit)
        return [_record_from_doc(d) for d in cursor]

    def count(self, *, owner_id: str) -> int:
        return self._col.count_documents({"owner_id": owner_id})

    def update(self, document_id: str, *, owner_id: str, fields: dict[str, Any]) -> DocumentRecord:
        fields = {**fields, "updated_at": utcnow()}
        doc = self._col.find_one_and_update(
            {"_id": to_object_id(document_id), "owner_id": owner_id},
            {"$set": fields},
            return_document=True,
        )
        if doc is None:
            raise NotFoundError("Document not found.")
        return _record_from_doc(doc)

    def bump_review_revision(self, document_id: str, *, owner_id: str) -> int:
        """Atomically increment and return the review revision.

        Callers bump BEFORE they change findings. If the process dies in between,
        the revision has moved on (so any export authorization is already invalid)
        but no decision changed: the failure direction is "too cautious", never
        "a stale approval still works".
        """
        doc = self._col.find_one_and_update(
            {"_id": to_object_id(document_id), "owner_id": owner_id},
            {"$inc": {"review_revision": 1}, "$set": {"updated_at": utcnow()}},
            return_document=True,
        )
        if doc is None:
            raise NotFoundError("Document not found.")
        return int(doc["review_revision"])

    def find_stuck_processing(self) -> list[dict[str, Any]]:
        """Documents left in 'processing' by a crashed process (any owner)."""
        return list(self._col.find({"status": DocumentStatus.PROCESSING.value}))

    def delete(self, document_id: str, *, owner_id: str) -> int:
        result = self._col.delete_one({"_id": to_object_id(document_id), "owner_id": owner_id})
        return result.deleted_count


class PageRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[PAGES]

    def replace_for_document(
        self, document_id: str, *, owner_id: str, version: int, pages: list[ExtractedPage]
    ) -> int:
        """Replace all stored pages for a document (used by first extraction and retries)."""
        oid = to_object_id(document_id)
        self._col.delete_many({"document_id": oid, "owner_id": owner_id})
        if not pages:
            return 0
        docs = [
            {
                "document_id": oid,
                "owner_id": owner_id,
                "document_version": version,
                "page_number": p.page_number,
                "geometry": p.geometry.model_dump(),
                "kind": p.kind,
                "supported": p.supported,
                "word_count": p.word_count,
                "image_count": p.image_count,
                "image_coverage": p.image_coverage,
                "text": p.text,
                "words": [w.model_dump() for w in p.words],
            }
            for p in pages
        ]
        result = self._col.insert_many(docs)
        return len(result.inserted_ids)

    def list_summaries(self, document_id: str, *, owner_id: str) -> list[PageSummary]:
        cursor = self._col.find(
            {"document_id": to_object_id(document_id), "owner_id": owner_id},
            projection={"text": 0, "words": 0},
        ).sort("page_number", ASCENDING)
        return [PageSummary.model_validate(d) for d in cursor]

    def list_full(self, document_id: str, *, owner_id: str) -> list[PageText]:
        """All pages including text and word boxes (used by search and detectors)."""
        cursor = self._col.find(
            {"document_id": to_object_id(document_id), "owner_id": owner_id}
        ).sort("page_number", ASCENDING)
        return [PageText.model_validate(d) for d in cursor]

    def get_page(self, document_id: str, page_number: int, *, owner_id: str) -> PageText:
        doc = self._col.find_one(
            {
                "document_id": to_object_id(document_id),
                "owner_id": owner_id,
                "page_number": page_number,
            }
        )
        if doc is None:
            raise NotFoundError("Page not found.")
        return PageText.model_validate(doc)

    def delete_for_document(self, document_id: str, *, owner_id: str) -> int:
        result = self._col.delete_many({"document_id": to_object_id(document_id), "owner_id": owner_id})
        return result.deleted_count
