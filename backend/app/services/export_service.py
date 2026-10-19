"""Export workflow: authorize -> redact -> verify -> store -> download.

Why a separate "authorization" step?
------------------------------------
Exporting is the one irreversible-looking action, so it needs an explicit, narrow
grant from the human reviewer. ``issue_authorization`` is a reviewer-only endpoint
(never exposed to the model/MCP tools). The token it returns is:

* stored only as a SHA-256 hash (a database leak does not leak usable tokens),
* valid for 5 minutes,
* pinned to the document's content hash, review revision and the exact set of
  approved rectangles - if anything changes, the token stops working,
* consumed atomically, so two simultaneous requests cannot both run an export,
* idempotent: re-sending the same token after it was used returns the *same*
  export record instead of creating another file,
* issued *for* one audience: the reviewer's own export (REST token) or the assistant
  (the reviewer clicked "Let the assistant export"). The two are never interchangeable,
  so the assistant cannot use the moment around the reviewer's own export click.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.db import Mongo
from app.models.common import BBox, utcnow
from app.models.documents import DocumentRecord
from app.models.findings import (
    ExportAudience,
    ExportAuthorization,
    ExportRecord,
    ExportRequest,
    ExportStatus,
    Finding,
    RetainedCopies,
    ReviewStatus,
    VerificationCheck,
)
from app.repositories.documents import DocumentRepository
from app.repositories.files import FileStorage
from app.repositories.review import (
    AuditRepository,
    AuthorizationRepository,
    ExportRepository,
    FindingRepository,
)
from app.services.errors import AppError, ConflictError
from app.services.redaction import ExportBlocked, build_redacted_pdf, verify_redaction
from app.services.review_service import ReviewService

logger = logging.getLogger(__name__)

AUTHORIZATION_TTL = timedelta(minutes=5)

SCOPE_STATEMENT = (
    "This export removes text, image pixels and vector graphics under the rectangles you approved, "
    "and strips metadata, attachments, annotations, form fields, bookmarks and earlier file revisions. "
    "It does not read text inside images, does not touch unapproved content (even identical text elsewhere), "
    "and cannot analyse scanned pages unless you acknowledge them. Verification results are listed on the export."
)


class AuthorizationError(AppError):
    status_code = 403
    code = "invalid_authorization"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def approved_digest(findings: list[Finding]) -> str:
    """Stable fingerprint of exactly what would be redacted."""
    parts = sorted(
        f"{f.id}:" + ",".join(f"{b.x0:.2f},{b.y0:.2f},{b.x1:.2f},{b.y1:.2f}" for b in f.boxes)
        for f in findings
        if f.status == ReviewStatus.APPROVED
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def neutral_export_filename(output_hash: str) -> str:
    """The name of an exported file. It says nothing about the document.

    The uploaded file's own name can contain exactly the sensitive text that was just redacted
    ("Jane Doe NDA.pdf"), so it must never be reused for the redacted copy. The first characters
    of the output's SHA-256 make the name unique and let a reviewer match a file to an export.
    """
    return f"redacted_{output_hash[:10]}.pdf"


class ExportService:
    def __init__(self, mongo: Mongo, settings: Settings) -> None:
        self._settings = settings
        self.documents = DocumentRepository(mongo.db)
        self.findings = FindingRepository(mongo.db)
        self.authorizations = AuthorizationRepository(mongo.db)
        self.exports = ExportRepository(mongo.db)
        self.audit = AuditRepository(mongo.db)
        self.files = FileStorage(mongo.pdf_bucket)
        self.review = ReviewService(mongo, settings)

    # ------------------------------------------------------------ authorization
    def issue_authorization(
        self, document_id: str, owner_id: str, audience: ExportAudience = ExportAudience.REVIEWER
    ) -> ExportAuthorization:
        record = self.documents.get(document_id, owner_id=owner_id)
        summary = self.review.summary(document_id, owner_id)
        if not summary.can_export:
            raise ConflictError(
                "Export is not allowed yet: " + " ".join(summary.export_blockers),
                code="export_blocked",
            )
        findings = self.findings.list(document_id, owner_id)
        token = secrets.token_urlsafe(32)
        expires = utcnow() + AUTHORIZATION_TTL
        self.authorizations.insert(
            {
                "_id": hash_token(token),
                "document_id": document_id,
                "owner_id": owner_id,
                "content_hash": record.content_hash,
                "review_revision": record.review_revision,
                "approved_digest": approved_digest(findings),
                "audience": audience.value,
                "created_at": utcnow(),
                "expires_at": expires,
                "consumed_at": None,
                "export_id": None,
            }
        )
        self.audit.record(
            document_id,
            owner_id,
            "export_authorized",
            {"review_revision": record.review_revision, "audience": audience.value},
        )
        return ExportAuthorization(
            token=token,
            document_id=document_id,
            review_revision=record.review_revision,
            expires_at=expires,
            audience=audience,
        )

    # ------------------------------------------------------------------- export
    def export(self, document_id: str, owner_id: str, req: ExportRequest) -> ExportRecord:
        """Reviewer path (REST): the caller presents the token the reviewer was given."""
        return self._export_authorized(
            document_id,
            owner_id,
            hash_token(req.authorization),
            acknowledge_unsupported_pages=req.acknowledge_unsupported_pages,
            audience=ExportAudience.REVIEWER,
        )

    def export_with_reviewer_authorization(self, document_id: str, owner_id: str) -> ExportRecord:
        """Assistant path (MCP): the model never sees or supplies a token.

        The reviewer clicks "Let the assistant export" in the UI, which issues an authorization
        for the assistant. This finds the newest such one for
        this document (issued within the last few minutes) and runs the exact same
        checks as the REST path: unexpired, unconsumed, and still matching the
        document hash, review revision and approved rectangles. Retrying after a
        success returns the same export, never a second one.

        The assistant can never acknowledge unsupported pages; that stays a human
        decision made in the UI.
        """
        self.documents.get(document_id, owner_id=owner_id)  # ownership first: unknown id -> not found
        auth = self.authorizations.find_latest_for_assistant(
            document_id, owner_id, created_after=utcnow() - AUTHORIZATION_TTL
        )
        if auth is None:
            raise AuthorizationError(
                "The reviewer has not let the assistant export. Ask the reviewer to click "
                "'Let the assistant export' on the hand-off card in the Chat tab, then try again.",
                code="authorization_required",
            )
        return self._export_authorized(
            document_id,
            owner_id,
            auth["_id"],
            acknowledge_unsupported_pages=False,
            audience=ExportAudience.ASSISTANT,
            actor="assistant",
        )

    def _export_authorized(
        self,
        document_id: str,
        owner_id: str,
        token_hash: str,
        *,
        acknowledge_unsupported_pages: bool,
        audience: ExportAudience,
        actor: str = "reviewer",
    ) -> ExportRecord:
        record = self.documents.get(document_id, owner_id=owner_id)
        auth = self.authorizations.get(token_hash, document_id=document_id, owner_id=owner_id)
        if auth is None:
            raise AuthorizationError("Unknown export authorization. Request a new one.")
        if auth.get("audience", ExportAudience.REVIEWER.value) != audience.value:
            # Each authorization works for exactly one kind of caller. Nothing is consumed.
            raise AuthorizationError(
                "This export authorization was issued for a different purpose. Request a new one.",
                code="authorization_wrong_audience",
            )

        # Retry of an already-used token: return the same export, never a second one.
        if auth.get("consumed_at") is not None:
            return self._existing_for_consumed(document_id, owner_id, auth)

        if _aware(auth["expires_at"]) <= utcnow():
            raise AuthorizationError("Export authorization expired. Request a new one.", code="authorization_expired")

        findings = self.findings.list(document_id, owner_id)
        if (
            auth["content_hash"] != record.content_hash
            or auth["review_revision"] != record.review_revision
            or auth["approved_digest"] != approved_digest(findings)
        ):
            raise ConflictError(
                "The review changed after this export was authorized. Request a new authorization.",
                code="authorization_stale",
            )

        summary = self.review.summary(document_id, owner_id)
        if not summary.can_export:
            raise ConflictError("Export is not allowed: " + " ".join(summary.export_blockers), code="export_blocked")
        unsupported = list(summary.unsupported_pages)
        if unsupported and not acknowledge_unsupported_pages:
            raise ConflictError(
                f"Pages {', '.join(map(str, unsupported))} could not be analysed (scanned or mixed). "
                "Acknowledge this to export anyway.",
                code="unsupported_pages_unacknowledged",
            )

        # Exactly one caller wins the right to run this export.
        if not self.authorizations.consume(token_hash, utcnow()):
            fresh = self.authorizations.get(token_hash, document_id=document_id, owner_id=owner_id) or auth
            return self._existing_for_consumed(document_id, owner_id, fresh)

        try:
            export_doc = self._run(document_id, owner_id, record, findings, unsupported)
        except Exception:
            logger.exception("Export crashed for %s", document_id)
            export_doc = self._failure_doc(owner_id, record, findings, "Export failed unexpectedly; nothing was stored.")
        export_id = self.exports.insert(export_doc)
        self.authorizations.attach_export(token_hash, export_id)
        self.audit.record(
            document_id,
            owner_id,
            "export_completed",
            {
                "export_id": export_id,
                "actor": actor,  # who pressed the button: the reviewer (REST) or the assistant (MCP)
                "status": export_doc["status"],
                "approved_count": export_doc["approved_count"],
                "failed_checks": [c["name"] for c in export_doc["checks"] if not c["passed"]],
            },
        )
        return self.get_export(document_id, owner_id, export_id)

    def _existing_for_consumed(self, document_id: str, owner_id: str, auth: dict[str, Any]) -> ExportRecord:
        export_id = auth.get("export_id")
        if export_id:
            return self.get_export(document_id, owner_id, export_id)
        raise ConflictError(
            "This authorization is already being used. Check the exports list or request a new authorization.",
            code="export_in_progress",
        )

    def _run(
        self,
        document_id: str,
        owner_id: str,
        record: DocumentRecord,
        findings: list[Finding],
        acknowledged: list[int],
    ) -> dict[str, Any]:
        approved = [f for f in findings if f.status == ReviewStatus.APPROVED]
        rejected = [f for f in findings if f.status == ReviewStatus.REJECTED]
        approved_by_page: dict[int, list[BBox]] = defaultdict(list)
        for f in approved:
            approved_by_page[f.page_number].extend(f.boxes)
        rejected_by_page: dict[int, list[BBox]] = defaultdict(list)
        for f in rejected:
            rejected_by_page[f.page_number].extend(f.boxes)
        approved_texts = sorted({f.text for f in approved if f.text and f.category.value != "MANUAL"})

        raw = self.documents.get_raw(document_id, owner_id=owner_id)
        original = self.files.get_bytes(raw["original_file_id"], owner_id=owner_id)

        base = self._base_doc(owner_id, record, findings, acknowledged)
        try:
            output = build_redacted_pdf(original, approved_by_page)
        except ExportBlocked as exc:
            base.update(status=ExportStatus.BLOCKED.value, checks=[
                VerificationCheck(name="export_allowed", passed=False, detail=exc.message).model_dump()
            ])
            return base

        # Re-read the stored original NOW so "original unchanged" is a real comparison.
        original_now = self.files.get_bytes(raw["original_file_id"], owner_id=owner_id)
        checks, retained = verify_redaction(
            original=original,
            output=output,
            expected_original_hash=record.content_hash,
            original_hash_now=hashlib.sha256(original_now).hexdigest(),
            approved=approved_by_page,
            rejected=rejected_by_page,
            approved_texts=approved_texts,
        )
        base["checks"] = [c.model_dump() for c in checks]
        base["retained"] = retained.model_dump()
        if all(c.passed for c in checks if c.required):
            output_hash = hashlib.sha256(output).hexdigest()
            filename = neutral_export_filename(output_hash)
            file_id = self.files.put(
                output,
                filename=filename,
                metadata={
                    "owner_id": owner_id,
                    "kind": "export",
                    "document_id": document_id,
                    "source_content_hash": record.content_hash,
                },
            )
            base.update(
                status=ExportStatus.VERIFIED.value,
                file_id=file_id,
                filename=filename,
                output_hash=output_hash,
                output_size_bytes=len(output),
            )
        else:
            base["status"] = ExportStatus.VERIFICATION_FAILED.value  # nothing is stored
        return base

    def _base_doc(
        self, owner_id: str, record: DocumentRecord, findings: list[Finding], acknowledged: list[int]
    ) -> dict[str, Any]:
        approved = [f for f in findings if f.status == ReviewStatus.APPROVED]
        return {
            "document_id": record.id,
            "owner_id": owner_id,
            "status": ExportStatus.VERIFICATION_FAILED.value,
            "review_revision": record.review_revision,
            "content_hash": record.content_hash,
            "approved_count": len(approved),
            "approved_finding_ids": [f.id for f in approved],
            "by_category": dict(Counter(f.category.value for f in approved)),
            "output_hash": None,
            "output_size_bytes": None,
            "filename": None,
            "file_id": None,
            "warnings_acknowledged_at": None,
            "checks": [],
            "retained": RetainedCopies().model_dump(),
            "unsupported_pages_acknowledged": acknowledged,
            "scope_statement": SCOPE_STATEMENT,
            "created_at": utcnow(),
        }

    def _failure_doc(
        self, owner_id: str, record: DocumentRecord, findings: list[Finding], message: str
    ) -> dict[str, Any]:
        doc = self._base_doc(owner_id, record, findings, [])
        doc["checks"] = [VerificationCheck(name="export_completed", passed=False, detail=message).model_dump()]
        return doc

    # --------------------------------------------------------------------- read
    def _to_record(self, doc: dict[str, Any], current: DocumentRecord) -> ExportRecord:
        stale = current.review_revision != doc["review_revision"] or current.content_hash != doc["content_hash"]
        status = ExportStatus(doc["status"])
        verified = status == ExportStatus.VERIFIED
        retained = RetainedCopies.model_validate(doc.get("retained") or {})
        # Copies of text the reviewer approved are still readable in the file: the reviewer must say they
        # understand before the file can be downloaded. (Other advisory warnings are informational only.)
        requires_ack = verified and retained.unresolved > 0
        acknowledged = doc.get("warnings_acknowledged_at") is not None
        output_hash = doc.get("output_hash")
        return ExportRecord(
            id=str(doc["_id"]),
            document_id=doc["document_id"],
            status=status,
            review_revision=doc["review_revision"],
            content_hash=doc["content_hash"],
            approved_count=doc["approved_count"],
            by_category=doc["by_category"],
            output_hash=doc.get("output_hash"),
            output_size_bytes=doc.get("output_size_bytes"),
            # Always derived from the output hash, never read back from storage: exports made before this
            # rule existed may have been stored under the uploaded file's name.
            filename=neutral_export_filename(output_hash) if output_hash else None,
            checks=[VerificationCheck.model_validate(c) for c in doc["checks"]],
            retained=retained,
            unsupported_pages_acknowledged=doc.get("unsupported_pages_acknowledged", []),
            scope_statement=doc.get("scope_statement", ""),
            stale=stale,
            has_warnings=any(not c.get("passed", True) and not c.get("required", True) for c in doc["checks"]),
            requires_acknowledgement=requires_ack,
            warnings_acknowledged=acknowledged,
            downloadable=verified and not stale and doc.get("file_id") is not None and (not requires_ack or acknowledged),
            created_at=doc["created_at"],
        )

    def get_export(self, document_id: str, owner_id: str, export_id: str) -> ExportRecord:
        current = self.documents.get(document_id, owner_id=owner_id)
        return self._to_record(self.exports.get_raw(document_id, owner_id, export_id), current)

    def list_exports(self, document_id: str, owner_id: str) -> list[ExportRecord]:
        current = self.documents.get(document_id, owner_id=owner_id)
        return [self._to_record(d, current) for d in self.exports.list_raw(document_id, owner_id)]

    def acknowledge_warnings(self, document_id: str, owner_id: str, export_id: str) -> ExportRecord:
        """The reviewer's statement: "I know other copies of approved text remain in this file".

        REST only. The assistant has no tool for this, so it can never unlock a download.
        Harmless to repeat, and a no-op for an export with nothing to acknowledge.
        """
        current = self.documents.get(document_id, owner_id=owner_id)
        raw = self.exports.get_raw(document_id, owner_id, export_id)
        record = self._to_record(raw, current)
        if record.status != ExportStatus.VERIFIED:
            raise ConflictError("This export failed verification; there is nothing to acknowledge.", code="export_not_verified")
        if record.stale:
            raise ConflictError(
                "The review changed after this export was created. Export again to get a current file.",
                code="export_stale",
            )
        if record.requires_acknowledgement and not record.warnings_acknowledged:
            self.exports.set_acknowledged(document_id, owner_id, export_id, utcnow())
            pages = sorted({int(loc["page"]) for loc in record.retained.unresolved_locations})
            # Counts and page numbers only; never text.
            self.audit.record(
                document_id,
                owner_id,
                "export_warnings_acknowledged",
                {"export_id": export_id, "unresolved_copies": record.retained.unresolved, "pages": pages},
            )
        return self._to_record(self.exports.get_raw(document_id, owner_id, export_id), current)

    def open_download(self, document_id: str, owner_id: str, export_id: str):
        """Return (record, stream). Refuses unverified or stale exports."""
        current = self.documents.get(document_id, owner_id=owner_id)
        raw = self.exports.get_raw(document_id, owner_id, export_id)
        record = self._to_record(raw, current)
        if record.status != ExportStatus.VERIFIED or raw.get("file_id") is None:
            raise ConflictError("This export failed verification and cannot be downloaded.", code="export_not_verified")
        if record.stale:
            raise ConflictError(
                "The review changed after this export was created. Export again to get a current file.",
                code="export_stale",
            )
        if record.requires_acknowledgement and not record.warnings_acknowledged:
            pages = sorted({int(loc["page"]) for loc in record.retained.unresolved_locations})
            raise ConflictError(
                f"This file still contains {record.retained.unresolved} other copy(ies) of text you approved for "
                f"redaction (page(s) {', '.join(map(str, pages))}) that no finding covers. Review them, or "
                "acknowledge them in the Export tab, before downloading.",
                code="warnings_unacknowledged",
            )
        stream = self.files.open_stream(raw["file_id"], owner_id=owner_id)
        self.audit.record(document_id, owner_id, "export_downloaded", {"export_id": export_id})
        return record, stream

