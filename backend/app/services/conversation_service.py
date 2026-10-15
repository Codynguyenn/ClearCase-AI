"""Conversation operations: create and read conversations, history and run details; recover after a restart.

Starting a run (which needs the assistant and tools) lives in ``app.agent.runs``.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.db import Mongo
from app.models.conversations import (
    AssistantMode,
    Conversation,
    MessagePage,
    MessageRole,
    MessageStatus,
    RunDetail,
)
from app.repositories.conversations import (
    ConversationRepository,
    MessageRepository,
    RunRepository,
    ToolEventRepository,
)
from app.repositories.documents import DocumentRepository

logger = logging.getLogger(__name__)

INTERRUPTED_NOTICE = "This request was interrupted because the server restarted. Please send it again."


def recover_interrupted_runs(mongo: Mongo) -> int:
    """At startup, fail runs left active by a previous process and leave a notice in their conversations.

    Without this a conversation would stay "busy" forever (one active run per conversation is enforced by the
    database). Proposals the run already made are kept: they are ordinary reviewable findings.
    """
    runs = RunRepository(mongo.db)
    messages = MessageRepository(mongo.db)
    stuck = runs.fail_all_active("Interrupted by a server restart.")
    for doc in stuck:
        messages.append(
            owner_id=doc["owner_id"],
            conversation_id=doc["conversation_id"],
            role=MessageRole.ASSISTANT,
            content=INTERRUPTED_NOTICE,
            status=MessageStatus.FAILED,
            run_id=str(doc["_id"]),
        )
    if stuck:
        logger.warning("Marked %d interrupted assistant run(s) as failed", len(stuck))
    return len(stuck)


class ConversationService:
    def __init__(self, mongo: Mongo, settings: Settings) -> None:
        self._settings = settings
        self._documents = DocumentRepository(mongo.db)
        self.conversations = ConversationRepository(mongo.db)
        self.messages = MessageRepository(mongo.db)
        self.runs = RunRepository(mongo.db)
        self.tool_events = ToolEventRepository(mongo.db)

    def create(self, owner_id: str, document_id: str, title: str | None = None) -> Conversation:
        self._documents.get(document_id, owner_id=owner_id)  # 404 unless this owner's document
        return self.conversations.create(
            owner_id=owner_id,
            document_id=document_id,
            mode=AssistantMode(self._settings.assistant_mode),
            title=title or "Conversation",
        )

    def list_for_document(self, owner_id: str, document_id: str) -> list[Conversation]:
        self._documents.get(document_id, owner_id=owner_id)
        return self.conversations.list_for_document(owner_id, document_id)

    def get(self, owner_id: str, conversation_id: str) -> Conversation:
        return self.conversations.get(owner_id, conversation_id)

    def history(self, owner_id: str, conversation_id: str, *, before_seq: int | None, limit: int) -> MessagePage:
        return self.messages.list_page(owner_id, conversation_id, before_seq=before_seq, limit=limit)

    def run_detail(self, owner_id: str, run_id: str) -> RunDetail:
        run = self.runs.get(owner_id, run_id)
        message = self.messages.get(owner_id, run.assistant_message_id) if run.assistant_message_id else None
        return RunDetail(run=run, tool_events=self.tool_events.list_for_run(owner_id, run_id), assistant_message=message)
