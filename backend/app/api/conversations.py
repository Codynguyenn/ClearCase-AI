"""Conversation endpoints: create and list conversations, read history, send a message (starts a run), poll and cancel runs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import ConversationSvc, OwnerId, Runs
from app.models.conversations import (
    Conversation,
    CreateConversationRequest,
    MessagePage,
    RunDetail,
    SendMessageRequest,
    SendResult,
)

router = APIRouter(tags=["conversations"])

DocumentId = Annotated[str, Path(min_length=1, max_length=64)]
ConversationId = Annotated[str, Path(min_length=1, max_length=64)]
RunId = Annotated[str, Path(min_length=1, max_length=64)]


@router.get("/documents/{document_id}/conversations", response_model=list[Conversation])
def list_conversations(document_id: DocumentId, service: ConversationSvc, owner_id: OwnerId) -> list[Conversation]:
    return service.list_for_document(owner_id, document_id)


@router.post("/documents/{document_id}/conversations", response_model=Conversation, status_code=status.HTTP_201_CREATED)
def create_conversation(
    document_id: DocumentId, body: CreateConversationRequest, service: ConversationSvc, owner_id: OwnerId
) -> Conversation:
    return service.create(owner_id, document_id, body.title)


@router.get("/conversations/{conversation_id}/messages", response_model=MessagePage)
def list_messages(
    conversation_id: ConversationId,
    service: ConversationSvc,
    owner_id: OwnerId,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    """The newest ``limit`` messages (or those before ``before_seq``), oldest first, for paging back through history."""
    return service.history(owner_id, conversation_id, before_seq=before_seq, limit=limit)


@router.post("/conversations/{conversation_id}/messages", response_model=SendResult, status_code=status.HTTP_202_ACCEPTED)
async def send_message(conversation_id: ConversationId, body: SendMessageRequest, runs: Runs, owner_id: OwnerId) -> SendResult:
    """Store the message and start the assistant. Returns at once; poll ``GET /runs/{id}`` for progress and the answer.

    409 ``run_active`` if the previous request in this conversation is still running; 409 ``assistant_not_configured``
    in live mode without a usable provider. Repeating a ``client_message_id`` returns the original (``duplicate``).
    """
    return await runs.start(
        owner_id=owner_id, conversation_id=conversation_id, content=body.content, client_message_id=body.client_message_id
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: RunId, service: ConversationSvc, owner_id: OwnerId) -> RunDetail:
    return service.run_detail(owner_id, run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunDetail, status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(run_id: RunId, runs: Runs, service: ConversationSvc, owner_id: OwnerId) -> RunDetail:
    await runs.cancel(owner_id, run_id)
    return service.run_detail(owner_id, run_id)
