"""Findings and review endpoints (reviewer-facing).

Approving, rejecting, drawing regions and adjusting are decisions that belong to the
human reviewer. These routes are the only way to make a finding ``approved``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import OwnerId, ReviewSvc
from app.models.findings import (
    AdjustRequest,
    BulkDecisionRequest,
    Category,
    DecisionRequest,
    DecisionResult,
    DetectionResult,
    DetectRequest,
    Finding,
    FindingsResponse,
    ManualRegionRequest,
    ProposalResult,
    ProposeFromDetectionRequest,
    ProposeFromSearchRequest,
    ReviewStatus,
    ReviewSummary,
)
from app.services.detection import detect_candidates

router = APIRouter(prefix="/documents/{document_id}", tags=["review"])

DocumentId = Annotated[str, Path(min_length=1, max_length=64)]
FindingId = Annotated[str, Path(min_length=1, max_length=80)]


@router.post("/detect", response_model=DetectionResult)
def detect(document_id: DocumentId, body: DetectRequest, service: ReviewSvc, owner_id: OwnerId) -> DetectionResult:
    """Preview what the detectors notice. Stores nothing: candidates are not proposals."""
    record = service.ready_document(document_id, owner_id)
    pages = service.pages.list_full(document_id, owner_id=owner_id)
    return detect_candidates(
        pages,
        document_id=document_id,
        content_hash=record.content_hash,
        categories=set(body.categories) if body.categories else None,
    )


@router.get("/findings", response_model=FindingsResponse)
def list_findings(
    document_id: DocumentId,
    service: ReviewSvc,
    owner_id: OwnerId,
    status_filter: Annotated[ReviewStatus | None, Query(alias="status")] = None,
    category: Category | None = None,
    page: Annotated[int | None, Query(ge=1)] = None,
) -> FindingsResponse:
    return service.list_findings(document_id, owner_id, status=status_filter, category=category, page_number=page)


@router.post("/findings/propose-detected", response_model=ProposalResult, status_code=status.HTTP_201_CREATED)
def propose_detected(
    document_id: DocumentId, body: ProposeFromDetectionRequest, service: ReviewSvc, owner_id: OwnerId
) -> ProposalResult:
    """Turn detector candidates for the chosen categories into *proposed* findings."""
    return service.propose_from_detection(document_id, owner_id, body.categories)


@router.post("/findings/propose-search", response_model=ProposalResult, status_code=status.HTTP_201_CREATED)
def propose_search(
    document_id: DocumentId, body: ProposeFromSearchRequest, service: ReviewSvc, owner_id: OwnerId
) -> ProposalResult:
    return service.propose_from_search(document_id, owner_id, body)


@router.post("/findings/manual", response_model=Finding, status_code=status.HTTP_201_CREATED)
def manual_region(
    document_id: DocumentId, body: ManualRegionRequest, service: ReviewSvc, owner_id: OwnerId
) -> Finding:
    return service.add_manual_region(document_id, owner_id, body)


@router.post("/findings/bulk-decision", response_model=DecisionResult)
def bulk_decision(
    document_id: DocumentId, body: BulkDecisionRequest, service: ReviewSvc, owner_id: OwnerId
) -> DecisionResult:
    return service.bulk_decide(document_id, owner_id, body.finding_ids, body.action)


@router.post("/findings/{finding_id}/decision", response_model=DecisionResult)
def decide(
    document_id: DocumentId, finding_id: FindingId, body: DecisionRequest, service: ReviewSvc, owner_id: OwnerId
) -> DecisionResult:
    return service.decide(
        document_id, owner_id, finding_id, body.action, apply_to_all_matching=body.apply_to_all_matching
    )


@router.put("/findings/{finding_id}/boxes", response_model=Finding)
def adjust(
    document_id: DocumentId, finding_id: FindingId, body: AdjustRequest, service: ReviewSvc, owner_id: OwnerId
) -> Finding:
    """Replace the rectangles. The finding returns to 'proposed' and must be re-approved."""
    return service.adjust(document_id, owner_id, finding_id, body.boxes)


@router.get("/review-summary", response_model=ReviewSummary)
def review_summary(document_id: DocumentId, service: ReviewSvc, owner_id: OwnerId) -> ReviewSummary:
    return service.summary(document_id, owner_id)
