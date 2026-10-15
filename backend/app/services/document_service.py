"""Document lifecycle: upload -> store -> extract -> ready/unsupported/error, plus delete.

Why a service layer? The API route should only parse the request and shape the
response. The MCP tools (Phase 3) and tests need the same behaviour without HTTP,
so the real logic lives here and both call it.
"""

from __future__ import annotations

import hashlib
import logging
import re
import traceback

from bson import ObjectId

from app.config import Settings
from app.db import Mongo
from app.models.common import utcnow
from app.models.documents import DocumentRecord, DocumentStatus
from app.repositories.documents import DocumentRepository, PageRepository
from app.repositories.files import FileStorage
from app.repositories.review import (
    AuditRepository,
    AuthorizationRepository,
    ExportRepository,
    FindingRepository,
    ReviewRevisionRepository,
)
from app.services.errors import ConflictError, ValidationFailed
from app.services.pdf_processing import PdfValidationError, extract_document, validate_pdf

logger = logging.getLogger(__name__)

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")


def sanitize_filename(name: str | None) -> str:
    """Keep only the base name with a conservative character set.

    The original name is user input: it could contain path separators, control
    characters or header-breaking characters. We never use it to touch the
    filesystem, but we still store and echo a cleaned version.
    """
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    base = _UNSAFE_FILENAME_CHARS.sub("_", base)
    base = base.strip(" .")
    if not base:
        base = "document.pdf"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base[:200]


class DocumentService:
    def __init__(self, mongo: Mongo, settings: Settings) -> None:
        self._settings = settings
        self.documents = DocumentRepository(mongo.db)
        self.pages = PageRepository(mongo.db)
        self.files = FileStorage(mongo.pdf_bucket)
        self._mongo = mongo
        self._findings = FindingRepository(mongo.db)
        self._revisions = ReviewRevisionRepository(mongo.db)
        self._exports = ExportRepository(mongo.db)
        self._authorizations = AuthorizationRepository(mongo.db)
        self._audit = AuditRepository(mongo.db)

    def _review(self):
        # Imported lazily: ReviewService does not depend on DocumentService, but this keeps
        # the module graph simple and avoids any import cycle.
        from app.services.review_service import ReviewService

        return ReviewService(self._mongo, self._settings)

    # ------------------------------------------------------------------ upload
    def upload(self, *, owner_id: str, filename: str | None, data: bytes) -> DocumentRecord:
        try:
            info = validate_pdf(
                data,
                max_bytes=self._settings.max_upload_bytes,
                max_pages=self._settings.max_pages,
            )
        except PdfValidationError as exc:
            # 413 ("payload too large") is the accurate status for an over-size file; every other
            # rejection is a 422 about the content.
            raise ValidationFailed(
                exc.message, code=exc.code, status_code=413 if exc.code == "file_too_large" else None
            ) from exc

        safe_name = sanitize_filename(filename)
        content_hash = hashlib.sha256(data).hexdigest()
        file_id = self.files.put(
            data,
            filename=safe_name,
            metadata={"owner_id": owner_id, "content_hash": content_hash, "kind": "original"},
        )
        record = self.documents.insert(
            owner_id=owner_id,
            filename=safe_name,
            content_hash=content_hash,
            size_bytes=len(data),
            page_count=info.page_count,
            original_file_id=file_id,
        )
        logger.info("Stored document %s (%d bytes, %d pages)", record.id, len(data), info.page_count)
        return record

    # ----------------------------------------------------------------- process
    def process(self, document_id: str, *, owner_id: str) -> DocumentRecord:
        """Run extraction for a stored document. Safe to call again after an error."""
        raw = self.documents.get_raw(document_id, owner_id=owner_id)
        if raw["status"] == DocumentStatus.PROCESSING.value:
            raise ConflictError("Document is already being processed.")

        attempts = int(raw.get("processing", {}).get("attempts", 0)) + 1
        self.documents.update(
            document_id,
            owner_id=owner_id,
            fields={
                "status": DocumentStatus.PROCESSING.value,
                "processing": {
                    "attempts": attempts,
                    "started_at": utcnow(),
                    "finished_at": None,
                    "error": None,
                },
            },
        )

        try:
            data = self.files.get_bytes(raw["original_file_id"], owner_id=owner_id)
            result = extract_document(data, time_limit_seconds=self._settings.extraction_timeout_seconds)
            new_version = int(raw.get("version", 1)) + (1 if raw.get("extraction") else 0)
            self.pages.replace_for_document(
                document_id, owner_id=owner_id, version=new_version, pages=result.pages
            )
            status = DocumentStatus.READY if result.all_supported else DocumentStatus.UNSUPPORTED
            info = result.info.model_copy(update={"extracted_at": utcnow()})
            updated = self.documents.update(
                document_id,
                owner_id=owner_id,
                fields={
                    "status": status.value,
                    "version": new_version,
                    "page_count": len(result.pages),
                    "extraction": info.model_dump(),
                    "processing.finished_at": utcnow(),
                },
            )
            if raw.get("extraction"):
                # Re-extraction: findings whose text no longer matches become stale, and the
                # review revision moves so older export authorizations stop working.
                self._review().revalidate_after_reprocess(document_id, owner_id)
            return updated
        except Exception as exc:  # noqa: BLE001 - we persist the failure and re-raise for logs
            logger.error("Extraction failed for %s: %s\n%s", document_id, exc, traceback.format_exc())
            return self.documents.update(
                document_id,
                owner_id=owner_id,
                fields={
                    "status": DocumentStatus.ERROR.value,
                    "processing.finished_at": utcnow(),
                    # Store the error type and message only; never raw document text.
                    "processing.error": f"{type(exc).__name__}: {exc}"[:500],
                },
            )

    def recover_interrupted(self) -> int:
        """At startup, mark documents stuck in 'processing' as errors so users can retry."""
        stuck = self.documents.find_stuck_processing()
        for doc in stuck:
            self.documents.update(
                str(doc["_id"]),
                owner_id=doc["owner_id"],
                fields={
                    "status": DocumentStatus.ERROR.value,
                    "processing.finished_at": utcnow(),
                    "processing.error": "Processing was interrupted by a server restart. Retry processing.",
                },
            )
        if stuck:
            logger.warning("Marked %d interrupted document(s) as error", len(stuck))
        return len(stuck)

    # -------------------------------------------------------------------- read
    def get(self, document_id: str, *, owner_id: str) -> DocumentRecord:
        return self.documents.get(document_id, owner_id=owner_id)

    def original_file_id(self, document_id: str, *, owner_id: str) -> ObjectId:
        return self.documents.get_raw(document_id, owner_id=owner_id)["original_file_id"]

    # ------------------------------------------------------------------ delete
    def delete(self, document_id: str, *, owner_id: str) -> dict[str, int]:
        """Remove the document and everything derived from it.

        Later phases add findings, conversations, messages, exports and audit
        payloads to this cascade. The order matters a little: we delete the
        metadata record last so a crash midway leaves a record that still points
        at (possibly already removed) children rather than orphans with no parent.
        """
        raw = self.documents.get_raw(document_id, owner_id=owner_id)
        counts: dict[str, int] = {}
        export_files = 0
        for export in self._exports.list_raw(document_id, owner_id):
            if export.get("file_id") is not None and self.files.delete(export["file_id"]):
                export_files += 1
        counts["export_files"] = export_files
        counts["exports"] = self._exports.delete_for_document(document_id, owner_id)
        counts["export_authorizations"] = self._authorizations.delete_for_document(document_id, owner_id)
        counts["findings"] = self._findings.delete_for_document(document_id, owner_id)
        counts["review_revisions"] = self._revisions.delete_for_document(document_id, owner_id)
        counts["audit_events"] = self._audit.delete_for_document(document_id, owner_id)
        counts["pages"] = self.pages.delete_for_document(document_id, owner_id=owner_id)
        counts["files"] = 1 if self.files.delete(raw["original_file_id"]) else 0
        counts["documents"] = self.documents.delete(document_id, owner_id=owner_id)
        logger.info("Deleted document %s: %s", document_id, counts)
        return counts
