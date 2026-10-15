"""Repositories for findings, review revisions, export authorizations, exports and audit events.

As everywhere in this layer, every query includes ``owner_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from app.db import AUDIT_EVENTS, EXPORT_AUTHORIZATIONS, EXPORTS, FINDINGS, REVIEW_REVISIONS
from app.models.common import utcnow
from app.models.findings import Finding, ReviewStatus
from app.repositories.documents import to_object_id
from app.services.errors import NotFoundError


def _finding_from_doc(doc: dict[str, Any]) -> Finding:
    data = dict(doc)
    data["id"] = data.pop("_id")
    data.pop("owner_id", None)
    return Finding.model_validate(data)


class FindingRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[FINDINGS]

    def count_citing(self, owner_id: str, citation_ids: list[str]) -> int:
        """Findings of this owner that cite at least one of these policy passages (exact ids, so an index is used)."""
        if not citation_ids:
            return 0
        return self._col.count_documents({"owner_id": owner_id, "policy_refs": {"$in": citation_ids}})

    def existing_ids(self, document_id: str, owner_id: str, ids: list[str]) -> set[str]:
        cursor = self._col.find(
            {"_id": {"$in": ids}, "document_id": document_id, "owner_id": owner_id}, projection={"_id": 1}
        )
        return {d["_id"] for d in cursor}

    def insert_if_absent(self, doc: dict[str, Any]) -> bool:
        """Insert unless the deterministic id already exists. Returns True if created."""
        result = self._col.update_one({"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True)
        return result.upserted_id is not None

    def get_many(self, document_id: str, owner_id: str, ids: list[str]) -> list[Finding]:
        cursor = self._col.find({"_id": {"$in": ids}, "document_id": document_id, "owner_id": owner_id})
        found = {d["_id"]: _finding_from_doc(d) for d in cursor}
        return [found[i] for i in ids if i in found]

    def get(self, document_id: str, owner_id: str, finding_id: str) -> Finding:
        doc = self._col.find_one({"_id": finding_id, "document_id": document_id, "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Finding not found.")
        return _finding_from_doc(doc)

    def list(
        self,
        document_id: str,
        owner_id: str,
        *,
        status: ReviewStatus | None = None,
        category: str | None = None,
        page_number: int | None = None,
    ) -> list[Finding]:
        query: dict[str, Any] = {"document_id": document_id, "owner_id": owner_id}
        if status is not None:
            query["status"] = status.value
        if category is not None:
            query["category"] = category
        if page_number is not None:
            query["page_number"] = page_number
        cursor = self._col.find(query).sort(
            [("page_number", ASCENDING), ("char_start", ASCENDING), ("_id", ASCENDING)]
        )
        return [_finding_from_doc(d) for d in cursor]

    def set_status(self, document_id: str, owner_id: str, ids: list[str], status: ReviewStatus, now: datetime) -> None:
        self._col.update_many(
            {"_id": {"$in": ids}, "document_id": document_id, "owner_id": owner_id},
            {"$set": {"status": status.value, "decided_at": now, "updated_at": now}},
        )

    def set_boxes(
        self, document_id: str, owner_id: str, finding_id: str, boxes: list[dict[str, float]], now: datetime
    ) -> None:
        self._col.update_one(
            {"_id": finding_id, "document_id": document_id, "owner_id": owner_id},
            {
                "$set": {
                    "boxes": boxes,
                    "mappable": True,
                    "adjusted": True,
                    "status": ReviewStatus.PROPOSED.value,
                    "decided_at": None,
                    "updated_at": now,
                }
            },
        )

    def update_fields(self, document_id: str, owner_id: str, finding_id: str, fields: dict[str, Any]) -> None:
        self._col.update_one(
            {"_id": finding_id, "document_id": document_id, "owner_id": owner_id},
            {"$set": {**fields, "updated_at": utcnow()}},
        )

    def delete_for_document(self, document_id: str, owner_id: str) -> int:
        return self._col.delete_many({"document_id": document_id, "owner_id": owner_id}).deleted_count


class ReviewRevisionRepository:
    """Append-only log of review changes (ids and counts only; no document text)."""

    def __init__(self, db: Database) -> None:
        self._col = db[REVIEW_REVISIONS]

    def record(self, document_id: str, owner_id: str, revision: int, action: str, finding_ids: list[str]) -> None:
        self._col.insert_one(
            {
                "document_id": document_id,
                "owner_id": owner_id,
                "revision": revision,
                "action": action,
                "finding_ids": finding_ids[:500],
                "actor": "reviewer",
                "created_at": utcnow(),
            }
        )

    def list(self, document_id: str, owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
        cursor = (
            self._col.find({"document_id": document_id, "owner_id": owner_id}, projection={"_id": 0})
            .sort("revision", DESCENDING)
            .limit(limit)
        )
        return list(cursor)

    def delete_for_document(self, document_id: str, owner_id: str) -> int:
        return self._col.delete_many({"document_id": document_id, "owner_id": owner_id}).deleted_count


class AuthorizationRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[EXPORT_AUTHORIZATIONS]

    def insert(self, doc: dict[str, Any]) -> None:
        self._col.insert_one(doc)

    def get(self, token_hash: str, *, document_id: str, owner_id: str) -> dict[str, Any] | None:
        return self._col.find_one({"_id": token_hash, "document_id": document_id, "owner_id": owner_id})

    def find_latest(self, document_id: str, owner_id: str, *, created_after: datetime) -> dict[str, Any] | None:
        """The newest authorization the reviewer issued for this document within the window.

        Used by the MCP export tool, which never receives a token: the human issues
        one in the UI and the server finds it here.
        """
        return self._col.find_one(
            {"document_id": document_id, "owner_id": owner_id, "created_at": {"$gte": created_after}},
            sort=[("created_at", -1)],
        )

    def consume(self, token_hash: str, now: datetime) -> bool:
        """Atomically mark as used. Exactly one caller can win."""
        result = self._col.update_one({"_id": token_hash, "consumed_at": None}, {"$set": {"consumed_at": now}})
        return result.modified_count == 1

    def attach_export(self, token_hash: str, export_id: str) -> None:
        self._col.update_one({"_id": token_hash}, {"$set": {"export_id": export_id}})

    def delete_for_document(self, document_id: str, owner_id: str) -> int:
        return self._col.delete_many({"document_id": document_id, "owner_id": owner_id}).deleted_count


class ExportRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[EXPORTS]

    def insert(self, doc: dict[str, Any]) -> str:
        return str(self._col.insert_one(doc).inserted_id)

    def get_raw(self, document_id: str, owner_id: str, export_id: str) -> dict[str, Any]:
        try:
            oid = ObjectId(export_id)
        except Exception as exc:  # noqa: BLE001
            raise NotFoundError("Export not found.") from exc
        doc = self._col.find_one({"_id": oid, "document_id": document_id, "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Export not found.")
        return doc

    def list_raw(self, document_id: str, owner_id: str) -> list[dict[str, Any]]:
        return list(
            self._col.find({"document_id": document_id, "owner_id": owner_id}).sort("created_at", DESCENDING)
        )

    def set_acknowledged(self, document_id: str, owner_id: str, export_id: str, now: datetime) -> None:
        """Record the reviewer's acknowledgement once (a second call changes nothing)."""
        self._col.update_one(
            {"_id": ObjectId(export_id), "document_id": document_id, "owner_id": owner_id, "warnings_acknowledged_at": None},
            {"$set": {"warnings_acknowledged_at": now}},
        )

    def delete_for_document(self, document_id: str, owner_id: str) -> int:
        return self._col.delete_many({"document_id": document_id, "owner_id": owner_id}).deleted_count


class AuditRepository:
    """Audit events hold ids, counts and outcomes only; never document text."""

    def __init__(self, db: Database) -> None:
        self._col = db[AUDIT_EVENTS]

    def record(self, document_id: str, owner_id: str, event: str, details: dict[str, Any] | None = None) -> None:
        self._col.insert_one(
            {
                "document_id": document_id,
                "owner_id": owner_id,
                "event": event,
                "details": details or {},
                "created_at": utcnow(),
            }
        )

    def delete_for_document(self, document_id: str, owner_id: str) -> int:
        return self._col.delete_many({"document_id": document_id, "owner_id": owner_id}).deleted_count


__all__ = [
    "AuditRepository",
    "AuthorizationRepository",
    "ExportRepository",
    "FindingRepository",
    "ReviewRevisionRepository",
    "to_object_id",
]
