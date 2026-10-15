"""Policy and retrieval models (the reference-policy corpus, separate from reviewed documents)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.common import ApiModel


class PolicyStatus(StrEnum):
    ACTIVE = "active"  # the version retrieval searches
    ARCHIVED = "archived"  # kept (old citations still resolve) but not searched


class EmbeddingState(StrEnum):
    READY = "ready"  # every chunk embedded with the current model revision
    PARTIAL = "partial"  # some chunks missing or stale
    STALE = "stale"  # embedded with a different model revision: re-index needed
    UNAVAILABLE = "unavailable"  # no embedding model could be used when indexing
    PENDING = "pending"  # chunks stored, embedding not finished (e.g. a crash): re-index


class EmbeddingSummary(BaseModel):
    state: EmbeddingState
    model: str
    revision: str
    chunks_current: int = Field(description="chunks embedded with the model revision now configured")
    chunks_stale: int = Field(description="chunks embedded with some other revision (ignored by retrieval)")
    chunks_missing: int = Field(description="chunks with no embedding")
    detail: str | None = None


class PolicyRecord(ApiModel):
    id: str
    policy_id: str
    version: str
    title: str
    source: str
    is_fictional_example: bool
    status: PolicyStatus
    content_hash: str
    chunk_count: int
    word_count: int
    truncated_chunks: int = Field(
        default=0, description="chunks longer than the embedding model reads (the tail is ignored by retrieval)"
    )
    chunker_version: int = Field(description="version of the chunking rules this policy version was split with")
    chunker_outdated: bool = Field(
        description="true when the chunking rules have changed since; upload the text as a new version to re-split it"
    )
    cited_by_findings: int = Field(
        default=0, description="review findings that cite a passage of this version (they show 'unavailable' if it is deleted)"
    )
    embedding: EmbeddingSummary
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None = None


class PolicyPassage(ApiModel):
    """One policy chunk, addressable by a stable citation id such as ``northwind-redaction-policy:1.0:3``."""

    citation_id: str
    policy_id: str
    policy_title: str
    policy_version: str
    policy_status: PolicyStatus
    section: str
    source: str
    ordinal: int
    text: str
    is_fictional_example: bool


class RetrievedPassage(PolicyPassage):
    score: float = Field(description="cosine similarity to the query (a similarity heuristic, not a probability)")
    rank: int


class RetrievalStatus(StrEnum):
    OK = "ok"
    NO_RELEVANT_POLICY = "no_relevant_policy"  # policies exist but nothing is similar enough
    NO_POLICIES = "no_policies"  # nothing active to search
    UNAVAILABLE = "unavailable"  # embeddings cannot be produced; semantic retrieval did not run


class RetrievalMethod(BaseModel):
    kind: str = "semantic_cosine"
    model: str
    revision: str
    min_score: float
    weak_min_score: float | None = None
    note: str = "Cosine similarity over local embeddings; the cutoff is a heuristic tuned on a small synthetic dev set."


class RetrievalResult(BaseModel):
    status: RetrievalStatus
    message: str
    query: str
    passages: list[RetrievedPassage]
    method: RetrievalMethod
    chunks_considered: int = 0
    chunks_stale_ignored: int = 0
    weak_passages: list[RetrievedPassage] = Field(
        default_factory=list,
        description=(
            "FOR THE HUMAN REVIEWER ONLY. Passages scoring a little below the cutoff, offered when nothing reached it. "
            "They are weak matches, are never given to the assistant, and must not be cited as policy."
        ),
    )
    top_score_below_cutoff: float | None = Field(
        default=None, description="best score seen when nothing passed the cutoff (for transparency)"
    )


class PolicySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=20)


class ResolveRequest(BaseModel):
    citation_ids: list[str] = Field(min_length=1, max_length=50)


class ResolveResult(BaseModel):
    found: list[PolicyPassage]
    missing: list[str] = Field(description="ids that do not exist for this owner (never shown as citations)")
