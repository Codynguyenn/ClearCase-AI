"""Document and page models.

A *document* record is small metadata about an uploaded PDF. The PDF bytes live
in GridFS; extracted text lives in separate *page* records. Keeping them apart
means listing documents is cheap and a 200-page PDF never produces one giant
MongoDB document.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import ApiModel, BBox


class DocumentStatus(StrEnum):
    """Lifecycle of an uploaded document.

    uploaded    -> bytes stored, extraction not started
    processing  -> extraction running
    ready       -> every page extracted as text
    unsupported -> stored and inspected, but one or more pages cannot be analysed
                   (scanned/image-only or mixed). The user is told explicitly.
    error       -> extraction failed; the user can retry
    """

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


PageKind = Literal["text", "scanned", "mixed", "empty"]


class Word(BaseModel):
    """One extracted word with its rectangle and its position in the page text.

    ``char_start``/``char_end`` index into ``PageText.text`` so later features
    (search, entity detection) can map a character span back to rectangles without
    re-reading the PDF. This is the "span -> rectangle" mapping the spec requires.
    """

    text: str
    bbox: BBox
    block: int
    line: int
    word_index: int
    char_start: int
    char_end: int


class PageGeometry(BaseModel):
    """Facts needed to place rectangles correctly on a rendered page.

    PyMuPDF reports word coordinates in the *unrotated* page space, while the
    PDF viewer renders the page with its ``/Rotate`` value applied. Storing both
    the unrotated size and the rotation lets the frontend do one well-tested
    transform instead of guessing.
    """

    width: float = Field(description="unrotated page width in points")
    height: float = Field(description="unrotated page height in points")
    rotation: int = Field(description="page /Rotate value in degrees (0, 90, 180, 270)")


class PageSummary(ApiModel):
    page_number: int = Field(ge=1)
    geometry: PageGeometry
    kind: PageKind
    word_count: int
    image_count: int
    image_coverage: float = Field(ge=0, le=1, description="fraction of page area covered by images")
    supported: bool


class PageText(PageSummary):
    text: str
    words: list[Word]


class ExtractionInfo(BaseModel):
    engine: str
    engine_version: str
    extracted_at: datetime | None = None
    duration_ms: int | None = None
    supported_pages: int = 0
    unsupported_pages: list[int] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ProcessingInfo(BaseModel):
    """Persisted job status so a crash mid-extraction is visible and retryable."""

    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class DocumentRecord(ApiModel):
    id: str
    owner_id: str
    filename: str
    content_hash: str = Field(description="SHA-256 of the original bytes")
    size_bytes: int
    page_count: int
    status: DocumentStatus
    version: int = Field(default=1, description="increments when stored pages change")
    review_revision: int = Field(
        default=0, description="increments on every change to findings or decisions; export authorizations pin it"
    )
    extraction: ExtractionInfo | None = None
    processing: ProcessingInfo = Field(default_factory=ProcessingInfo)
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(ApiModel):
    items: list[DocumentRecord]
    total: int


class DeleteResponse(ApiModel):
    document_id: str
    deleted: dict[str, int] = Field(description="counts of removed records per collection")
