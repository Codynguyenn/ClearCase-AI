"""Findings (redaction proposals), detection candidates, review and export models.

Vocabulary
----------
* **Candidate** - something a detector noticed. Candidates are NOT proposals and
  are never redacted automatically.
* **Finding** - a stored redaction proposal for one occurrence. It starts as
  ``proposed``; only the reviewer (through the API, never the model) can make it
  ``approved``.
* **Review revision** - an integer on the document that increases every time the
  set of findings or any decision changes. Export authorizations are tied to it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.common import ApiModel, BBox


class Category(StrEnum):
    # People and organisations are separate on purpose so "keep company names" works.
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    LOCATION = "LOCATION"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    SSN = "SSN"
    ADDRESS = "ADDRESS"
    CUSTOM = "CUSTOM"  # text the user searched for and chose to redact
    MANUAL = "MANUAL"  # a region the reviewer drew by hand


class ReviewStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"  # the document was re-extracted and this span no longer matches


class Origin(StrEnum):
    DETECTOR = "detector"
    SEARCH = "search"
    MANUAL = "manual"
    AGENT = "agent"  # proposed by the assistant through an MCP tool; never auto-approved


class ReviewLabel(StrEnum):
    """Honest provenance label shown instead of a fake probability."""

    FORMAT_VALID = "format_valid"  # matched a pattern AND passed validation rules; not verified real
    REVIEW_NEEDED = "review_needed"  # detector has no calibrated confidence
    USER_SPECIFIED = "user_specified"
    MANUAL_REGION = "manual_region"


# --------------------------------------------------------------------- detection


class Candidate(ApiModel):
    candidate_id: str
    category: Category
    page_number: int
    char_start: int
    char_end: int
    text: str
    context: str
    boxes: list[BBox]
    mappable: bool
    partial_word: bool
    sources: list[str] = Field(description="detector ids, e.g. 'pattern:email', 'ner:spacy:en_core_web_sm@3.8.0'")
    label: ReviewLabel
    explanation: str


class DetectorInfo(ApiModel):
    id: str
    kind: str
    available: bool
    detail: str


class DetectionResult(ApiModel):
    candidates: list[Candidate]
    detectors: list[DetectorInfo]
    searched_pages: list[int]
    unsupported_pages: list[int]
    notes: list[str] = Field(default_factory=list)


class DetectRequest(BaseModel):
    categories: list[Category] | None = None  # None = all detector categories


# ---------------------------------------------------------------------- findings


class Finding(ApiModel):
    id: str
    document_id: str
    document_version: int
    content_hash: str
    page_number: int
    char_start: int | None = None  # None for manual regions
    char_end: int | None = None
    text: str = Field(description="sensitive: original text of the span")
    context: str = Field(description="sensitive: short surrounding text")
    boxes: list[BBox]
    original_boxes: list[BBox] = Field(default_factory=list, description="boxes before any reviewer adjustment")
    category: Category
    source: str
    sources: list[str] = Field(default_factory=list)
    origin: Origin
    label: ReviewLabel
    explanation: str
    policy_refs: list[str] = Field(default_factory=list)  # filled by RAG in Phase 4
    status: ReviewStatus
    mappable: bool
    partial_word: bool = False
    adjusted: bool = False
    group_key: str = Field(description="hash of category + normalised text; groups identical mentions")
    overlaps: list[str] = Field(default_factory=list, description="ids of overlapping findings (possibly other categories)")
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None


class ProposalInput(BaseModel):
    """What a caller may ask to propose. Coordinates are never accepted: the server
    re-derives text and rectangles from the stored document."""

    page_number: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    category: Category
    source: str = Field(max_length=120)
    sources: list[str] = Field(default_factory=list)
    origin: Origin
    label: ReviewLabel
    explanation: str = Field(default="", max_length=300)
    expected_text: str | None = Field(default=None, max_length=2000)
    policy_refs: list[str] = Field(default_factory=list)


class ProposeFromDetectionRequest(BaseModel):
    categories: list[Category] = Field(min_length=1)


class ProposeFromSearchRequest(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    case_sensitive: bool = False
    whole_word: bool = False
    category: Category = Category.CUSTOM
    occurrence_ids: list[str] | None = Field(default=None, max_length=500)
    all_matches: bool = Field(
        default=False, description="must be true to propose every match; otherwise occurrence_ids is required"
    )
    explanation: str = Field(default="", max_length=300)


class ManualRegionRequest(BaseModel):
    page_number: int = Field(ge=1)
    box: BBox
    explanation: str = Field(default="", max_length=300)


class DecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REOPEN = "reopen"  # back to proposed


class DecisionRequest(BaseModel):
    action: DecisionAction
    apply_to_all_matching: bool = Field(
        default=False,
        description="explicitly extend the decision to every finding with the same group_key",
    )


class BulkDecisionRequest(BaseModel):
    action: DecisionAction
    finding_ids: list[str] = Field(min_length=1, max_length=500)


class AdjustRequest(BaseModel):
    boxes: list[BBox] = Field(min_length=1, max_length=20)


class ProposalResult(ApiModel):
    created: list[Finding]
    existing: list[Finding] = Field(description="identical findings that already existed (not duplicated)")
    rejected_inputs: list[str] = Field(default_factory=list, description="why some inputs were refused")
    notes: list[str] = Field(default_factory=list)
    review_revision: int


class FindingsResponse(ApiModel):
    items: list[Finding]
    total: int
    review_revision: int


class DecisionResult(ApiModel):
    updated: list[Finding]
    review_revision: int


class ReviewSummary(ApiModel):
    document_id: str
    review_revision: int
    by_status: dict[str, int]
    by_category: dict[str, dict[str, int]]
    unresolved: int = Field(description="findings still in 'proposed'")
    unmappable_proposed: int
    unsupported_pages: list[int]
    approved: int
    can_export: bool
    export_blockers: list[str]
    warnings: list[str] = Field(default_factory=list)
    unproposed_copies: int = Field(
        default=0,
        description="occurrences of text you approved that no finding covers (counts only, no text)",
    )
    unproposed_locations: list[dict[str, int | str]] = Field(default_factory=list)


# ------------------------------------------------------------------------ export


class ExportAuthorization(ApiModel):
    token: str
    document_id: str
    review_revision: int
    expires_at: datetime


class ExportRequest(BaseModel):
    authorization: str = Field(min_length=20, max_length=200)
    acknowledge_unsupported_pages: bool = False


class VerificationCheck(ApiModel):
    name: str
    passed: bool
    detail: str
    required: bool = Field(default=True, description="a failed required check blocks download")


class RetainedCopies(ApiModel):
    """Text-search results for approved text elsewhere in the output.

    Only counts, pages and finding ids are stored; never the text itself.
    """

    intentionally_retained: int = 0
    unresolved: int = 0
    unresolved_locations: list[dict[str, int | str]] = Field(default_factory=list)


class ExportStatus(StrEnum):
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    BLOCKED = "blocked"


class ExportRecord(ApiModel):
    id: str
    document_id: str
    status: ExportStatus
    review_revision: int
    content_hash: str
    approved_count: int
    by_category: dict[str, int]
    output_hash: str | None = None
    output_size_bytes: int | None = None
    filename: str | None = None
    checks: list[VerificationCheck] = Field(default_factory=list)
    retained: RetainedCopies = Field(default_factory=RetainedCopies)
    unsupported_pages_acknowledged: list[int] = Field(default_factory=list)
    scope_statement: str = ""
    stale: bool = False
    downloadable: bool = False
    created_at: datetime
