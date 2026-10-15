"""Repositories for conversations, messages, assistant runs and tool activity.

As everywhere in this layer, every query includes ``owner_id``. Each record also carries ``document_id`` so
deleting a document can remove all of it without first walking the hierarchy.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.db import AGENT_RUNS, CONVERSATIONS, MESSAGES, TOOL_EVENTS
from app.models.common import utcnow
from app.models.conversations import (
    MAX_ERROR_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    AgentRun,
    AssistantMode,
    Citation,
    Conversation,
    Message,
    MessagePage,
    MessageRole,
    MessageStatus,
    RunStatus,
    ToolEvent,
    ToolEventStatus,
)
from app.repositories.documents import to_object_id
from app.services.errors import ConflictError, NotFoundError


def _with_id(doc: dict[str, Any]) -> dict[str, Any]:
    data = dict(doc)
    data["id"] = str(data.pop("_id"))
    data.pop("owner_id", None)
    return data


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[CONVERSATIONS]

    def create(self, *, owner_id: str, document_id: str, mode: AssistantMode, title: str) -> Conversation:
        now = utcnow()
        doc = {
            "owner_id": owner_id,
            "document_id": document_id,
            "title": title.strip()[:MAX_TITLE_CHARS] or "Conversation",
            "mode": mode.value,
            "next_seq": 1,  # the number the next message will take; bumped atomically
            "message_count": 0,
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }
        doc["_id"] = self._col.insert_one(doc).inserted_id
        return self._model(doc)

    @staticmethod
    def _model(doc: dict[str, Any]) -> Conversation:
        data = _with_id(doc)
        data.pop("next_seq", None)
        return Conversation.model_validate(data)

    def get_raw(self, owner_id: str, conversation_id: str) -> dict[str, Any]:
        doc = self._col.find_one({"_id": to_object_id(conversation_id), "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Conversation not found.", code="conversation_not_found")
        return doc

    def get(self, owner_id: str, conversation_id: str) -> Conversation:
        return self._model(self.get_raw(owner_id, conversation_id))

    def list_for_document(self, owner_id: str, document_id: str, *, limit: int = 20) -> list[Conversation]:
        cursor = self._col.find({"owner_id": owner_id, "document_id": document_id}).sort("updated_at", DESCENDING).limit(limit)
        return [self._model(d) for d in cursor]

    def take_seq(self, owner_id: str, conversation_id: str) -> int:
        """Atomically reserve the next message number. A crash after this leaves a harmless gap."""
        doc = self._col.find_one_and_update(
            {"_id": to_object_id(conversation_id), "owner_id": owner_id},
            {"$inc": {"next_seq": 1}},
            return_document=ReturnDocument.BEFORE,
        )
        if doc is None:
            raise NotFoundError("Conversation not found.", code="conversation_not_found")
        return int(doc["next_seq"])

    def touch(self, owner_id: str, conversation_id: str, *, messages_added: int = 0) -> None:
        self._col.update_one(
            {"_id": to_object_id(conversation_id), "owner_id": owner_id},
            {"$set": {"updated_at": utcnow()}, "$inc": {"version": 1, "message_count": messages_added}},
        )

    def delete_for_document(self, owner_id: str, document_id: str) -> int:
        return self._col.delete_many({"owner_id": owner_id, "document_id": document_id}).deleted_count


class MessageRepository:
    def __init__(self, db: Database) -> None:
        self._col = db[MESSAGES]
        self._conversations = ConversationRepository(db)

    @staticmethod
    def _model(doc: dict[str, Any]) -> Message:
        return Message.model_validate(_with_id(doc))

    def append(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        role: MessageRole,
        content: str,
        status: MessageStatus = MessageStatus.COMPLETE,
        run_id: str | None = None,
        client_message_id: str | None = None,
        citations: list[Citation] | None = None,
    ) -> tuple[Message, bool]:
        """Store one message. Returns ``(message, created)``.

        If ``client_message_id`` was already used in this conversation, nothing new is stored and the original
        message comes back with ``created=False``: that is what makes a retried request safe.
        """
        conversation = self._conversations.get_raw(owner_id, conversation_id)  # 404 unless owned
        if client_message_id is not None:
            existing = self._col.find_one({"conversation_id": conversation_id, "client_message_id": client_message_id})
            if existing is not None:
                return self._model(existing), False

        seq = self._conversations.take_seq(owner_id, conversation_id)
        now = utcnow()
        doc = {
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "document_id": conversation["document_id"],
            "seq": seq,
            "role": role.value,
            "content": content,
            "status": status.value,
            "citations": [c.model_dump(mode="json") for c in (citations or [])],
            "run_id": run_id,
            "client_message_id": client_message_id,
            "created_at": now,
            "updated_at": now,
            "version": 1,
        }
        try:
            doc["_id"] = self._col.insert_one(doc).inserted_id
        except DuplicateKeyError:
            # Two identical requests raced; the database's unique index let exactly one through.
            winner = self._col.find_one({"conversation_id": conversation_id, "client_message_id": client_message_id})
            if winner is None:
                raise
            return self._model(winner), False
        self._conversations.touch(owner_id, conversation_id, messages_added=1)
        return self._model(doc), True

    def find_by_client_id(self, owner_id: str, conversation_id: str, client_message_id: str) -> Message | None:
        doc = self._col.find_one(
            {"owner_id": owner_id, "conversation_id": conversation_id, "client_message_id": client_message_id}
        )
        return self._model(doc) if doc else None

    def get(self, owner_id: str, message_id: str) -> Message:
        doc = self._col.find_one({"_id": to_object_id(message_id), "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Message not found.", code="message_not_found")
        return self._model(doc)

    def list_page(
        self, owner_id: str, conversation_id: str, *, before_seq: int | None = None, limit: int = 50
    ) -> MessagePage:
        """The ``limit`` newest messages older than ``before_seq`` (or the newest overall), oldest first."""
        self._conversations.get_raw(owner_id, conversation_id)  # 404 unless owned
        query: dict[str, Any] = {"owner_id": owner_id, "conversation_id": conversation_id}
        if before_seq is not None:
            query["seq"] = {"$lt": before_seq}
        rows = list(self._col.find(query).sort("seq", DESCENDING).limit(limit + 1))
        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        return MessagePage(messages=[self._model(d) for d in rows], has_more=has_more)

    def recent_for_model(self, owner_id: str, conversation_id: str, *, limit: int) -> list[tuple[str, str]]:
        """``(role, text)`` of the last ``limit`` normal messages, oldest first: what a model may be shown.

        App-written error notices are left out; they are not part of the conversation the model had.
        """
        rows = list(
            self._col.find(
                {"owner_id": owner_id, "conversation_id": conversation_id, "status": MessageStatus.COMPLETE.value}
            )
            .sort("seq", DESCENDING)
            .limit(limit)
        )
        rows.reverse()
        return [(d["role"], d["content"]) for d in rows]

    def delete_for_document(self, owner_id: str, document_id: str) -> int:
        return self._col.delete_many({"owner_id": owner_id, "document_id": document_id}).deleted_count


class RunRepository:
    """Assistant runs. The database itself allows at most one active run per conversation."""

    def __init__(self, db: Database) -> None:
        self._col = db[AGENT_RUNS]

    @staticmethod
    def _model(doc: dict[str, Any]) -> AgentRun:
        data = _with_id(doc)
        data.pop("active", None)
        return AgentRun.model_validate(data)

    def create(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        document_id: str,
        mode: AssistantMode,
        provider: str | None = None,
        model: str | None = None,
    ) -> AgentRun:
        doc = {
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "document_id": document_id,
            "mode": mode.value,
            "provider": provider,
            "model": model,
            "status": RunStatus.QUEUED.value,
            "active": True,  # the unique partial index on (conversation_id) applies while this is true
            "rounds": 0,
            "tool_calls": 0,
            "limits_hit": [],
            "error_code": None,
            "error": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "request_ids": [],
            "unverified_citations": 0,
            "assistant_message_id": None,
            "created_at": utcnow(),
            "started_at": None,
            "finished_at": None,
        }
        try:
            doc["_id"] = self._col.insert_one(doc).inserted_id
        except DuplicateKeyError as exc:
            raise ConflictError(
                "The assistant is still working on the previous request in this conversation.", code="run_active"
            ) from exc
        return self._model(doc)

    def get(self, owner_id: str, run_id: str) -> AgentRun:
        doc = self._col.find_one({"_id": to_object_id(run_id), "owner_id": owner_id})
        if doc is None:
            raise NotFoundError("Run not found.", code="run_not_found")
        return self._model(doc)

    def active_for_conversation(self, owner_id: str, conversation_id: str) -> AgentRun | None:
        doc = self._col.find_one({"owner_id": owner_id, "conversation_id": conversation_id, "active": True})
        return self._model(doc) if doc else None

    def active_for_document(self, owner_id: str, document_id: str) -> AgentRun | None:
        doc = self._col.find_one({"owner_id": owner_id, "document_id": document_id, "active": True})
        return self._model(doc) if doc else None

    def list_for_conversation(self, owner_id: str, conversation_id: str, *, limit: int = 50) -> list[AgentRun]:
        cursor = self._col.find({"owner_id": owner_id, "conversation_id": conversation_id}).sort("created_at", DESCENDING).limit(limit)
        return [self._model(d) for d in cursor]

    def mark_running(self, owner_id: str, run_id: str) -> None:
        self._col.update_one(
            {"_id": to_object_id(run_id), "owner_id": owner_id, "active": True},
            {"$set": {"status": RunStatus.RUNNING.value, "started_at": utcnow()}},
        )

    def finish(
        self,
        owner_id: str,
        run_id: str,
        *,
        status: RunStatus,
        rounds: int = 0,
        tool_calls: int = 0,
        limits_hit: list[str] | None = None,
        error_code: str | None = None,
        error: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        request_ids: list[str] | None = None,
        unverified_citations: int = 0,
        assistant_message_id: str | None = None,
    ) -> AgentRun:
        if status in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise ValueError("finish() needs a final status")
        doc = self._col.find_one_and_update(
            {"_id": to_object_id(run_id), "owner_id": owner_id},
            {
                "$set": {
                    "status": status.value,
                    "active": False,
                    "rounds": rounds,
                    "tool_calls": tool_calls,
                    "limits_hit": limits_hit or [],
                    "error_code": error_code,
                    "error": error[:MAX_ERROR_CHARS] if error else None,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "request_ids": (request_ids or [])[:50],
                    "unverified_citations": unverified_citations,
                    "assistant_message_id": assistant_message_id,
                    "finished_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise NotFoundError("Run not found.", code="run_not_found")
        return self._model(doc)

    def fail_all_active(self, reason: str) -> list[dict[str, Any]]:
        """Startup recovery: any run still active belongs to a process that no longer exists.

        Returns the raw records of the runs that were failed (the caller leaves a notice in each conversation).
        """
        stuck = list(self._col.find({"active": True}))
        for doc in stuck:
            self._col.update_one(
                {"_id": doc["_id"], "active": True},
                {
                    "$set": {
                        "status": RunStatus.FAILED.value,
                        "active": False,
                        "error_code": "interrupted",
                        "error": reason[:MAX_ERROR_CHARS],
                        "finished_at": utcnow(),
                    }
                },
            )
        return stuck

    def delete_for_document(self, owner_id: str, document_id: str) -> int:
        return self._col.delete_many({"owner_id": owner_id, "document_id": document_id}).deleted_count


class ToolEventRepository:
    """Sanitized activity rows. ``summary`` is built from counts and page numbers, never from document text."""

    def __init__(self, db: Database) -> None:
        self._col = db[TOOL_EVENTS]

    @staticmethod
    def _model(doc: dict[str, Any]) -> ToolEvent:
        return ToolEvent.model_validate(_with_id(doc))

    def record(
        self,
        *,
        owner_id: str,
        run_id: str,
        document_id: str,
        seq: int,
        tool: str,
        status: ToolEventStatus,
        duration_ms: int = 0,
        summary: str = "",
        error_code: str | None = None,
    ) -> ToolEvent:
        doc = {
            "owner_id": owner_id,
            "run_id": run_id,
            "document_id": document_id,
            "seq": seq,
            "tool": tool[:80],
            "status": status.value,
            "duration_ms": max(0, int(duration_ms)),
            "summary": summary[:MAX_SUMMARY_CHARS],
            "error_code": error_code,
            "created_at": utcnow(),
        }
        doc["_id"] = self._col.insert_one(doc).inserted_id
        return self._model(doc)

    def list_for_run(self, owner_id: str, run_id: str) -> list[ToolEvent]:
        cursor = self._col.find({"owner_id": owner_id, "run_id": run_id}).sort("seq", ASCENDING)
        return [self._model(d) for d in cursor]

    def delete_for_document(self, owner_id: str, document_id: str) -> int:
        return self._col.delete_many({"owner_id": owner_id, "document_id": document_id}).deleted_count


__all__ = [
    "ConversationRepository",
    "MessageRepository",
    "RunRepository",
    "ToolEventRepository",
]
