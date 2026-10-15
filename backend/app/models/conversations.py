"""Conversation, message, run and tool-activity models (Phase 5; see docs/phase5_design.md).

What is stored, and what deliberately is not:

* Messages are separate records (``seq`` orders them), never one ever-growing array.
* Only user and assistant *text* is stored as messages. Raw tool results contain document text, so they are
  never persisted; each tool call leaves a sanitized ``ToolEvent`` (tool, status, duration, a short summary).
* No chain-of-thought is stored anywhere.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.common import ApiModel

MAX_USER_MESSAGE_CHARS = 4000
MAX_ASSISTANT_MESSAGE_CHARS = 20000
MAX_SUMMARY_CHARS = 300
MAX_ERROR_CHARS = 500
MAX_TITLE_CHARS = 120


class AssistantMode(StrEnum):
    DEMO = "demo"  # scripted commands, no model
    LIVE = "live"  # a hosted model drives the tools


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    COMPLETE = "complete"
    FAILED = "failed"  # an error notice written by the app, not model output
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolEventStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class CitationKind(StrEnum):
    PAGE = "page"
    POLICY = "policy"


class Citation(BaseModel):
    kind: CitationKind
    # A page number ("3") or a policy passage id ("northwind-redaction-policy:1.0:3").
    ref: str = Field(max_length=120)
    # True only if code confirmed the page/passage was really returned by a tool in the same run (and, for a
    # quoted sentence, that the quote really appears in the passage).
    verified: bool = False
    quote: str | None = Field(default=None, max_length=500)


class Conversation(ApiModel):
    id: str
    document_id: str
    title: str
    mode: AssistantMode  # the mode it was created in; every run records its own
    message_count: int = 0
    created_at: datetime
    updated_at: datetime
    version: int = 1


class Message(ApiModel):
    id: str
    conversation_id: str
    document_id: str
    seq: int = Field(ge=1)
    role: MessageRole
    content: str
    status: MessageStatus = MessageStatus.COMPLETE
    citations: list[Citation] = Field(default_factory=list)
    run_id: str | None = None
    # Set by the client on user messages so a retried request cannot start a second run.
    client_message_id: str | None = None
    created_at: datetime
    updated_at: datetime
    version: int = 1


class MessagePage(ApiModel):
    messages: list[Message]  # oldest first
    has_more: bool  # older messages exist before the first one returned


class AgentRun(ApiModel):
    id: str
    conversation_id: str
    document_id: str
    mode: AssistantMode
    provider: str | None = None
    model: str | None = None
    status: RunStatus
    rounds: int = 0  # model calls made
    tool_calls: int = 0
    limits_hit: list[str] = Field(default_factory=list)  # e.g. "max_rounds", "max_seconds"
    error_code: str | None = None
    error: str | None = None  # sanitized: never raw document text or a key
    input_tokens: int = 0  # tokens not served from the prompt cache
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_ids: list[str] = Field(default_factory=list)  # provider request ids, for support questions
    unverified_citations: int = 0
    assistant_message_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ToolEvent(ApiModel):
    id: str
    run_id: str
    document_id: str
    seq: int = Field(ge=1)
    tool: str
    status: ToolEventStatus
    duration_ms: int = 0
    # Built from counts and page numbers only, never from document text.
    summary: str = ""
    error_code: str | None = None
    created_at: datetime


class CreateConversationRequest(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_USER_MESSAGE_CHARS)
    # Generate one per send in the client; repeating it (a retry after a network error) returns the original.
    client_message_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class SendResult(ApiModel):
    message: Message
    run: AgentRun
    duplicate: bool = False  # true when client_message_id had been used: nothing new was started


class RunDetail(ApiModel):
    run: AgentRun
    tool_events: list[ToolEvent]
    assistant_message: Message | None = None  # present once the run has finished
