"""Review workflow: propose -> reviewer decides -> (export in export_service).

Authority rules enforced here (and why they matter)
---------------------------------------------------
* **Proposals never arrive approved.** ``propose`` always stores ``status=proposed``.
  Only ``decide`` (called from the reviewer-facing API) can approve. The model and
  its tools (Phase 3+) are only ever given ``propose``.
* **The server derives locations.** Callers send a page and a character span; the
  server re-reads the stored page, checks the text still matches what the caller
  expected, and computes the rectangles itself. Client- or model-supplied
  coordinates are never trusted.
* **Every change bumps the review revision first.** If the process crashes midway,
  the revision has already moved, so any outstanding export authorization is
  invalid (the safe failure direction).
* **Identical proposals are deduplicated by id**, so a retried request cannot
  create duplicates, while overlapping proposals with *different* categories are
  both kept and flagged via ``overlaps``.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict

from app.config import Settings
from app.db import Mongo
from app.models.common import BBox, utcnow
from app.models.documents import DocumentRecord, DocumentStatus
from app.models.findings import (
    Category,
    DecisionAction,
    DecisionResult,
    Finding,
    FindingsResponse,
    ManualRegionRequest,
    Origin,
    ProposalInput,
    ProposalResult,
    ProposeFromSearchRequest,
    ReviewLabel,
    ReviewStatus,
    ReviewSummary,
)
from app.repositories.documents import DocumentRepository, PageRepository
from app.repositories.review import FindingRepository, ReviewRevisionRepository
from app.services.coverage import any_overlap, find_copies
from app.services.detection import detect_candidates
from app.services.errors import ConflictError, NotFoundError, ValidationFailed
from app.services.ids import group_key, make_finding_id, make_manual_finding_id
from app.services.search import search_pages
from app.services.spans import locate_span, normalize_text, snippet

logger = logging.getLogger(__name__)

MIN_BOX_SIZE = 1.0  # points
BOX_TOLERANCE = 0.5


class ReviewService:
    def __init__(self, mongo: Mongo, settings: Settings) -> None:
        self._settings = settings
        self.documents = DocumentRepository(mongo.db)
        self.pages = PageRepository(mongo.db)
        self.findings = FindingRepository(mongo.db)
        self.revisions = ReviewRevisionRepository(mongo.db)

    # --------------------------------------------------------------- helpers
    def ready_document(self, document_id: str, owner_id: str) -> DocumentRecord:
        record = self.documents.get(document_id, owner_id=owner_id)
        if record.status not in (DocumentStatus.READY, DocumentStatus.UNSUPPORTED):
            raise ConflictError(
                f"Document is '{record.status.value}'; wait for processing to finish or retry it.",
                code="document_not_ready",
            )
        return record

    @staticmethod
    def _attach_overlaps(items: list[Finding]) -> list[Finding]:
        by_page: dict[int, list[Finding]] = defaultdict(list)
        for f in items:
            if f.char_start is not None and f.char_end is not None and f.status != ReviewStatus.STALE:
                by_page[f.page_number].append(f)
        for group in by_page.values():
            for a in group:
                a.overlaps = [
                    b.id
                    for b in group
                    if b.id != a.id and a.char_start < b.char_end and b.char_start < a.char_end  # type: ignore[operator]
                ]
        return items

    # ----------------------------------------------------------------- list
    def list_findings(
        self,
        document_id: str,
        owner_id: str,
        *,
        status: ReviewStatus | None = None,
        category: Category | None = None,
        page_number: int | None = None,
    ) -> FindingsResponse:
        record = self.documents.get(document_id, owner_id=owner_id)
        everything = self._attach_overlaps(self.findings.list(document_id, owner_id))
        items = [
            f
            for f in everything
            if (status is None or f.status == status)
            and (category is None or f.category == category)
            and (page_number is None or f.page_number == page_number)
        ]
        return FindingsResponse(items=items, total=len(items), review_revision=record.review_revision)

    # -------------------------------------------------------------- propose
    def propose(
        self, document_id: str, owner_id: str, inputs: list[ProposalInput], *, action: str = "propose"
    ) -> ProposalResult:
        record = self.ready_document(document_id, owner_id)
        pages = {p.page_number: p for p in self.pages.list_full(document_id, owner_id=owner_id)}

        prepared: dict[str, tuple[ProposalInput, str, object, object]] = {}
        refused: list[str] = []
        for n, inp in enumerate(inputs, start=1):
            page = pages.get(inp.page_number)
            if page is None or not page.words:
                refused.append(f"Input {n}: page {inp.page_number} has no extracted text.")
                continue
            if inp.char_start >= inp.char_end or inp.char_end > len(page.text):
                refused.append(f"Input {n}: span is outside the page text.")
                continue
            text = page.text[inp.char_start : inp.char_end]
            if inp.expected_text is not None and normalize_text(inp.expected_text) != normalize_text(text):
                refused.append(f"Input {n}: text at that location no longer matches (document may have changed).")
                continue
            loc = locate_span(page, inp.char_start, inp.char_end)
            fid = make_finding_id(
                document_id, record.content_hash, inp.page_number, inp.char_start, inp.char_end, inp.category.value
            )
            if fid in prepared:  # same span+category twice in one request
                continue
            prepared[fid] = (inp, text, loc, page)

        existing_ids = self.findings.existing_ids(document_id, owner_id, list(prepared))
        new_items = {fid: v for fid, v in prepared.items() if fid not in existing_ids}

        revision = record.review_revision
        created_ids: list[str] = []
        if new_items:
            revision = self.documents.bump_review_revision(document_id, owner_id=owner_id)
            now = utcnow()
            for fid, (inp, text, loc, page) in new_items.items():
                boxes = [b.model_dump() for b in loc.boxes]  # type: ignore[attr-defined]
                doc = {
                    "_id": fid,
                    "owner_id": owner_id,
                    "document_id": document_id,
                    "document_version": record.version,
                    "content_hash": record.content_hash,
                    "page_number": inp.page_number,
                    "char_start": inp.char_start,
                    "char_end": inp.char_end,
                    "text": text,
                    "context": snippet(page.text, inp.char_start, inp.char_end),  # type: ignore[attr-defined]
                    "boxes": boxes,
                    "original_boxes": boxes,
                    "category": inp.category.value,
                    "source": inp.source,
                    "sources": inp.sources or [inp.source],
                    "origin": inp.origin.value,
                    "label": inp.label.value,
                    "explanation": inp.explanation or "Proposed for review.",
                    "policy_refs": inp.policy_refs,
                    "status": ReviewStatus.PROPOSED.value,
                    "mappable": loc.mappable,  # type: ignore[attr-defined]
                    "partial_word": loc.partial_word,  # type: ignore[attr-defined]
                    "adjusted": False,
                    "group_key": group_key(inp.category.value, text),
                    "created_at": now,
                    "updated_at": now,
                    "decided_at": None,
                }
                if self.findings.insert_if_absent(doc):
                    created_ids.append(fid)
            if created_ids:
                self.revisions.record(document_id, owner_id, revision, action, created_ids)

        everything = {f.id: f for f in self._attach_overlaps(self.findings.list(document_id, owner_id))}
        return ProposalResult(
            created=[everything[i] for i in created_ids if i in everything],
            existing=[everything[i] for i in prepared if i in existing_ids and i in everything],
            rejected_inputs=refused,
            review_revision=revision,
        )

    def propose_from_detection(self, document_id: str, owner_id: str, categories: list[Category]) -> ProposalResult:
        record = self.ready_document(document_id, owner_id)
        pages = self.pages.list_full(document_id, owner_id=owner_id)
        result = detect_candidates(
            pages, document_id=document_id, content_hash=record.content_hash, categories=set(categories)
        )
        inputs = [
            ProposalInput(
                page_number=c.page_number,
                char_start=c.char_start,
                char_end=c.char_end,
                category=c.category,
                source=c.sources[0],
                sources=c.sources,
                origin=Origin.DETECTOR,
                label=c.label,
                explanation=c.explanation,
                expected_text=c.text,
            )
            for c in result.candidates
        ]
        proposal = self.propose(document_id, owner_id, inputs, action="propose_detected")
        proposal.notes = [n for n in result.notes if "suggestions only" not in n]
        return proposal

    def propose_from_search(self, document_id: str, owner_id: str, req: ProposeFromSearchRequest) -> ProposalResult:
        record = self.ready_document(document_id, owner_id)
        if not req.all_matches and not req.occurrence_ids:
            raise ValidationFailed(
                "Choose specific occurrence_ids, or set all_matches=true to propose every match explicitly.",
                code="selection_required",
            )
        pages = self.pages.list_full(document_id, owner_id=owner_id)
        result = search_pages(
            pages,
            req.q,
            document_id=document_id,
            document_version=record.version,
            case_sensitive=req.case_sensitive,
            whole_word=req.whole_word,
        )
        chosen = result.occurrences
        refused: list[str] = []
        if req.occurrence_ids and not req.all_matches:
            wanted = set(req.occurrence_ids)
            chosen = [o for o in result.occurrences if o.occurrence_id in wanted]
            missing = wanted - {o.occurrence_id for o in chosen}
            if missing:
                refused.append(f"{len(missing)} occurrence id(s) were not found for this query.")
        inputs = [
            ProposalInput(
                page_number=o.page_number,
                char_start=o.char_start,
                char_end=o.char_end,
                category=req.category,
                source="search:literal",
                origin=Origin.SEARCH,
                label=ReviewLabel.USER_SPECIFIED,
                explanation=req.explanation or "Matches text you searched for.",
                expected_text=o.text,
            )
            for o in chosen
        ]
        proposal = self.propose(document_id, owner_id, inputs, action="propose_search")
        proposal.rejected_inputs = refused + proposal.rejected_inputs
        return proposal

    # --------------------------------------------------------- manual region
    def add_manual_region(self, document_id: str, owner_id: str, req: ManualRegionRequest) -> Finding:
        """A region the reviewer drew. Created already approved: drawing it IS the decision.

        This endpoint is reviewer-facing only and is deliberately not available to the
        model/MCP tools.
        """
        record = self.ready_document(document_id, owner_id)
        page = self.pages.get_page(document_id, req.page_number, owner_id=owner_id)
        box = self._validate_box(req.box, page.geometry.width, page.geometry.height)
        fid = make_manual_finding_id(document_id, record.content_hash, req.page_number, (box.x0, box.y0, box.x1, box.y1))
        if fid in self.findings.existing_ids(document_id, owner_id, [fid]):
            return self.findings.get(document_id, owner_id, fid)

        revision = self.documents.bump_review_revision(document_id, owner_id=owner_id)
        now = utcnow()
        dumped = [box.model_dump()]
        self.findings.insert_if_absent(
            {
                "_id": fid,
                "owner_id": owner_id,
                "document_id": document_id,
                "document_version": record.version,
                "content_hash": record.content_hash,
                "page_number": req.page_number,
                "char_start": None,
                "char_end": None,
                "text": "",
                "context": "",
                "boxes": dumped,
                "original_boxes": dumped,
                "category": Category.MANUAL.value,
                "source": "manual",
                "sources": ["manual"],
                "origin": Origin.MANUAL.value,
                "label": ReviewLabel.MANUAL_REGION.value,
                "explanation": req.explanation or "Region drawn by the reviewer.",
                "policy_refs": [],
                "status": ReviewStatus.APPROVED.value,
                "mappable": True,
                "partial_word": False,
                "adjusted": False,
                "group_key": group_key("MANUAL", fid),
                "created_at": now,
                "updated_at": now,
                "decided_at": now,
            }
        )
        self.revisions.record(document_id, owner_id, revision, "manual_region", [fid])
        return self.findings.get(document_id, owner_id, fid)

    @staticmethod
    def _validate_box(box: BBox, width: float, height: float) -> BBox:
        values = (box.x0, box.y0, box.x1, box.y1)
        if not all(math.isfinite(v) for v in values):
            raise ValidationFailed("Box coordinates must be finite numbers.", code="invalid_box")
        if box.x0 > box.x1 or box.y0 > box.y1:
            raise ValidationFailed("Box must have x0<x1 and y0<y1.", code="invalid_box")
        if box.x0 < -BOX_TOLERANCE or box.y0 < -BOX_TOLERANCE or box.x1 > width + BOX_TOLERANCE or box.y1 > height + BOX_TOLERANCE:
            raise ValidationFailed("Box lies outside the page.", code="box_outside_page")
        clamped = BBox(x0=max(0.0, box.x0), y0=max(0.0, box.y0), x1=min(width, box.x1), y1=min(height, box.y1))
        if clamped.width() < MIN_BOX_SIZE or clamped.height() < MIN_BOX_SIZE:
            raise ValidationFailed(f"Box must be at least {MIN_BOX_SIZE:g} point in each direction.", code="box_too_small")
        return clamped

    # --------------------------------------------------------------- decide
    def decide(
        self, document_id: str, owner_id: str, finding_id: str, action: DecisionAction, *, apply_to_all_matching: bool
    ) -> DecisionResult:
        self.documents.get(document_id, owner_id=owner_id)
        target = self.findings.get(document_id, owner_id, finding_id)
        if apply_to_all_matching:
            targets = [
                f for f in self.findings.list(document_id, owner_id) if f.group_key == target.group_key
            ]
            return self._apply(document_id, owner_id, targets, action, strict=False, label="decide_all_matching")
        return self._apply(document_id, owner_id, [target], action, strict=True, label="decide")

    def bulk_decide(self, document_id: str, owner_id: str, ids: list[str], action: DecisionAction) -> DecisionResult:
        self.documents.get(document_id, owner_id=owner_id)
        unique = list(dict.fromkeys(ids))
        found = self.findings.get_many(document_id, owner_id, unique)
        if len(found) != len(unique):
            raise NotFoundError("One or more findings were not found for this document.")
        return self._apply(document_id, owner_id, found, action, strict=False, label="bulk_decide")

    def _apply(
        self, document_id: str, owner_id: str, targets: list[Finding], action: DecisionAction, *, strict: bool, label: str
    ) -> DecisionResult:
        new_status = {
            DecisionAction.APPROVE: ReviewStatus.APPROVED,
            DecisionAction.REJECT: ReviewStatus.REJECTED,
            DecisionAction.REOPEN: ReviewStatus.PROPOSED,
        }[action]
        to_update: list[str] = []
        for f in targets:
            if f.status == ReviewStatus.STALE:
                if strict:
                    raise ConflictError(
                        "This finding is stale because the document was re-processed; propose it again.",
                        code="stale_finding",
                    )
                continue
            if new_status == ReviewStatus.APPROVED and not (f.mappable and f.boxes):
                if strict:
                    raise ConflictError(
                        "This finding could not be located on the page. Adjust it to set a region before approving.",
                        code="needs_manual_review",
                    )
                continue
            if f.status != new_status:
                to_update.append(f.id)

        record = self.documents.get(document_id, owner_id=owner_id)
        revision = record.review_revision
        if to_update:
            revision = self.documents.bump_review_revision(document_id, owner_id=owner_id)
            self.findings.set_status(document_id, owner_id, to_update, new_status, utcnow())
            self.revisions.record(document_id, owner_id, revision, f"{label}:{action.value}", to_update)
        updated = self.findings.get_many(document_id, owner_id, to_update) if to_update else []
        return DecisionResult(updated=updated, review_revision=revision)

    # --------------------------------------------------------------- adjust
    def adjust(self, document_id: str, owner_id: str, finding_id: str, boxes: list[BBox]) -> Finding:
        """Replace a finding's rectangles. Resets it to 'proposed': the reviewer must re-approve
        what will actually be removed."""
        self.documents.get(document_id, owner_id=owner_id)
        finding = self.findings.get(document_id, owner_id, finding_id)
        if finding.status == ReviewStatus.STALE:
            raise ConflictError("Stale findings cannot be adjusted.", code="stale_finding")
        page = self.pages.get_page(document_id, finding.page_number, owner_id=owner_id)
        validated = [self._validate_box(b, page.geometry.width, page.geometry.height) for b in boxes]
        revision = self.documents.bump_review_revision(document_id, owner_id=owner_id)
        self.findings.set_boxes(document_id, owner_id, finding_id, [b.model_dump() for b in validated], utcnow())
        self.revisions.record(document_id, owner_id, revision, "adjust", [finding_id])
        return self.findings.get(document_id, owner_id, finding_id)

    # -------------------------------------------------------------- summary
    def summary(self, document_id: str, owner_id: str) -> ReviewSummary:
        record = self.documents.get(document_id, owner_id=owner_id)
        items = self.findings.list(document_id, owner_id)
        by_status = Counter(f.status.value for f in items)
        by_category: dict[str, Counter] = defaultdict(Counter)
        for f in items:
            by_category[f.category.value][f.status.value] += 1

        unsupported = list(record.extraction.unsupported_pages) if record.extraction else []
        proposed = [f for f in items if f.status == ReviewStatus.PROPOSED]
        approved = [f for f in items if f.status == ReviewStatus.APPROVED]

        blockers: list[str] = []
        warnings: list[str] = []
        if record.status not in (DocumentStatus.READY, DocumentStatus.UNSUPPORTED):
            blockers.append(f"Document is '{record.status.value}'.")
        if not approved:
            blockers.append("No approved redactions yet.")
        if proposed:
            blockers.append(f"{len(proposed)} proposal(s) are undecided. Approve or reject each one.")
        if unsupported:
            warnings.append(
                f"Pages {', '.join(map(str, unsupported))} are scanned or mixed and were not analysed. "
                "Exporting requires acknowledging that they may contain unredacted sensitive content."
            )
        if by_status.get(ReviewStatus.STALE.value):
            warnings.append(f"{by_status[ReviewStatus.STALE.value]} finding(s) are stale and will be ignored.")

        unproposed_locations: list[dict[str, int | str]] = []
        approved_texts = sorted({f.text for f in approved if f.text and f.category != Category.MANUAL})
        if approved_texts:
            pages = self.pages.list_full(document_id, owner_id=owner_id)
            all_boxes: dict[int, list[BBox]] = defaultdict(list)
            for f in items:
                all_boxes[f.page_number].extend(f.boxes)
            for copy in find_copies(pages, approved_texts, document_id=document_id):
                if not any_overlap(copy.boxes, all_boxes.get(copy.page_number, [])):
                    unproposed_locations.append({"page": copy.page_number, "group": copy.text_index})
        if unproposed_locations:
            warnings.append(
                f"{len(unproposed_locations)} other occurrence(s) of text you approved are not covered by any "
                "finding. Search for the text and propose them, or leave them knowingly."
            )

        return ReviewSummary(
            document_id=document_id,
            review_revision=record.review_revision,
            by_status=dict(by_status),
            by_category={k: dict(v) for k, v in by_category.items()},
            unresolved=len(proposed),
            unmappable_proposed=sum(1 for f in proposed if not f.mappable),
            unsupported_pages=unsupported,
            approved=len(approved),
            can_export=not blockers,
            export_blockers=blockers,
            warnings=warnings,
            unproposed_copies=len(unproposed_locations),
            unproposed_locations=unproposed_locations,
        )

    # ------------------------------------------------- re-extraction handling
    def revalidate_after_reprocess(self, document_id: str, owner_id: str) -> int:
        """After pages are re-extracted, keep findings whose text still matches; mark the rest stale.

        Returns the number of findings changed. The review revision is bumped whenever any
        finding exists so older export authorizations cannot survive a re-extraction.
        """
        record = self.documents.get(document_id, owner_id=owner_id)
        items = self.findings.list(document_id, owner_id)
        if not items:
            return 0
        pages = {p.page_number: p for p in self.pages.list_full(document_id, owner_id=owner_id)}
        self.documents.bump_review_revision(document_id, owner_id=owner_id)
        changed = 0
        for f in items:
            if f.char_start is None:  # manual regions are geometry, not text: unaffected
                self.findings.update_fields(document_id, owner_id, f.id, {"document_version": record.version})
                continue
            page = pages.get(f.page_number)
            still_matches = page is not None and page.text[f.char_start : f.char_end] == f.text
            if still_matches:
                self.findings.update_fields(document_id, owner_id, f.id, {"document_version": record.version})
            else:
                self.findings.update_fields(
                    document_id,
                    owner_id,
                    f.id,
                    {"status": ReviewStatus.STALE.value, "document_version": record.version},
                )
                changed += 1
        logger.info("Revalidated findings for %s: %d became stale", document_id, changed)
        return changed
