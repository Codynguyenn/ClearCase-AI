"""MongoDB access for policies and their chunks.

Every method takes ``owner_id`` and filters by it, exactly like the document
repositories. Policies are a separate corpus from the legal documents being
reviewed: they live in their own collections and are never mixed into document
queries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING
from pymongo.database import Database

from app.db import POLICIES, POLICY_CHUNKS
from app.services.errors import NotFoundError


def policy_key(policy_id: str, version: str) -> str:
    return f"{policy_id}:{version}"


def citation_id(policy_id: str, version: str, ordinal: int) -> str:
    return f"{policy_id}:{version}:{ordinal}"


class PolicyRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[POLICIES]

    def insert(self, doc: dict[str, Any]) -> dict[str, Any]:
        result = self._col.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc

    def find(self, owner_id: str, policy_id: str, version: str) -> dict[str, Any] | None:
        return self._col.find_one({"owner_id": owner_id, "policy_id": policy_id, "version": version})

    def get(self, owner_id: str, policy_id: str, version: str) -> dict[str, Any]:
        doc = self.find(owner_id, policy_id, version)
        if doc is None:
            raise NotFoundError("Policy version not found.", code="policy_not_found")
        return doc

    def get_by_object_id(self, owner_id: str, object_id: str) -> dict[str, Any]:
        try:
            oid = ObjectId(object_id)
        except (InvalidId, TypeError) as exc:
            raise NotFoundError("Policy not found.", code="policy_not_found") from exc
        doc = self._col.find_one({"_id": oid, "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Policy not found.", code="policy_not_found")
        return doc

    def list(self, owner_id: str) -> list[dict[str, Any]]:
        return list(self._col.find({"owner_id": owner_id}).sort([("policy_id", ASCENDING), ("created_at", -1)]))

    def owners(self) -> list[str]:
        """Every owner that has at least one policy (used by the startup re-index, which covers all of them)."""
        return sorted(self._col.distinct("owner_id"))

    def versions(self, owner_id: str, policy_id: str) -> list[dict[str, Any]]:
        return list(self._col.find({"owner_id": owner_id, "policy_id": policy_id}))

    def active_keys(self, owner_id: str) -> list[str]:
        """The ``policy_id:version`` pairs retrieval may search: filtered by owner and status first."""
        return [policy_key(d["policy_id"], d["version"]) for d in self._col.find({"owner_id": owner_id, "status": "active"})]

    def set_status(self, owner_id: str, policy_id: str, version: str, status: str, now: datetime) -> None:
        fields: dict[str, Any] = {"status": status, "updated_at": now}
        if status == "active":
            fields["activated_at"] = now
        self._col.update_one({"owner_id": owner_id, "policy_id": policy_id, "version": version}, {"$set": fields})

    def update_fields(self, owner_id: str, policy_id: str, version: str, fields: dict[str, Any]) -> None:
        self._col.update_one({"owner_id": owner_id, "policy_id": policy_id, "version": version}, {"$set": fields})

    def delete(self, owner_id: str, policy_id: str, version: str | None) -> int:
        query: dict[str, Any] = {"owner_id": owner_id, "policy_id": policy_id}
        if version is not None:
            query["version"] = version
        return self._col.delete_many(query).deleted_count


class PolicyChunkRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[POLICY_CHUNKS]

    def insert_many(self, docs: list[dict[str, Any]]) -> None:
        if docs:
            self._col.insert_many(docs)

    def list_for_policy(self, owner_id: str, policy_id: str, version: str) -> list[dict[str, Any]]:
        return list(
            self._col.find({"owner_id": owner_id, "policy_key": policy_key(policy_id, version)}).sort("ordinal", ASCENDING)
        )

    def set_embedding(
        self, chunk_object_id: ObjectId, vector: list[float], *, model: str, revision: str, embedded_at: datetime
    ) -> None:
        self._col.update_one(
            {"_id": chunk_object_id},
            {
                "$set": {
                    "embedding": vector,
                    "embedding_model": model,
                    "embedding_revision": revision,
                    "embedded_at": embedded_at,
                }
            },
        )

    def set_truncated(self, chunk_object_id: ObjectId, truncated: bool) -> None:
        self._col.update_one({"_id": chunk_object_id}, {"$set": {"truncated_by_model": truncated}})

    def get_by_citation(self, owner_id: str, cite: str) -> dict[str, Any] | None:
        return self._col.find_one({"owner_id": owner_id, "citation_id": cite}, projection={"embedding": 0})

    def get_many_by_citation(self, owner_id: str, cites: list[str]) -> list[dict[str, Any]]:
        return list(self._col.find({"owner_id": owner_id, "citation_id": {"$in": cites}}, projection={"embedding": 0}))

    def candidates(self, owner_id: str, keys: list[str], revision: str) -> list[dict[str, Any]]:
        """Chunks to rank: owner and active policy versions are filtered *before* any scoring,
        and only chunks embedded with the current model revision are comparable."""
        return list(
            self._col.find(
                {
                    "owner_id": owner_id,
                    "policy_key": {"$in": keys},
                    "embedding_revision": revision,
                    "embedding": {"$type": "array"},
                }
            )
        )

    def count_stale(self, owner_id: str, keys: list[str], revision: str) -> int:
        return self._col.count_documents(
            {
                "owner_id": owner_id,
                "policy_key": {"$in": keys},
                "embedding": {"$type": "array"},
                "embedding_revision": {"$ne": revision},
            }
        )

    def count_by_state(self, owner_id: str, key: str, revision: str) -> tuple[int, int, int]:
        """(current, stale, missing) embedding counts for one policy version."""
        base = {"owner_id": owner_id, "policy_key": key}
        current = self._col.count_documents({**base, "embedding": {"$type": "array"}, "embedding_revision": revision})
        stale = self._col.count_documents({**base, "embedding": {"$type": "array"}, "embedding_revision": {"$ne": revision}})
        missing = self._col.count_documents({**base, "embedding": {"$not": {"$type": "array"}}})
        return current, stale, missing

    def delete_for_policy(self, owner_id: str, policy_id: str, version: str | None) -> int:
        query: dict[str, Any] = {"owner_id": owner_id, "policy_id": policy_id}
        if version is not None:
            query["policy_version"] = version
        return self._col.delete_many(query).deleted_count
