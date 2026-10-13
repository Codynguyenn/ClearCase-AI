"""PDF validation and text extraction using PyMuPDF.

This module is deliberately free of database or HTTP concerns so it can be unit
tested with in-memory PDF bytes.

Key behaviours (plain English):

* ``validate_pdf`` checks the *actual* bytes, not the filename or the browser's
  content-type, both of which are trivially spoofed. It rejects non-PDFs,
  malformed files, encrypted files, empty files and files over the size/page caps
  with a specific error code the API can turn into a helpful message.
* ``extract_document`` pulls words with rectangles from each page and builds a
  plain-text version of the page whose character offsets point back at those
  words. That span->rectangle map is what later lets us turn "the name on line 3"
  into a precise redaction box without asking a language model for coordinates.
* Each page is classified as text / scanned / mixed / empty using simple,
  explainable heuristics (word count and image coverage). Anything that is not a
  plain text page is flagged as unsupported rather than silently analysed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pymupdf

from app.models.common import BBox
from app.models.documents import (
    ExtractionInfo,
    PageGeometry,
    PageKind,
    Word,
)

ENGINE_NAME = "pymupdf"
ENGINE_VERSION = pymupdf.__version__

# Heuristic thresholds. They are intentionally simple and documented; the
# evaluation phase can revisit them with measurements.
SCANNED_IMAGE_COVERAGE = 0.5  # no text + images covering >= 50% of page => scanned
MIXED_IMAGE_COVERAGE = 0.3  # text + images covering >= 30% of page => mixed

# Expand ligatures ("ﬁ" -> "fi") so search behaves as users expect; keep the
# other default word-extraction flags.
_WORD_FLAGS = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_PRESERVE_LIGATURES


class PdfValidationError(Exception):
    """Raised when an upload is rejected. ``code`` is stable for clients/tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PdfInfo:
    page_count: int


def validate_pdf(data: bytes, *, max_bytes: int, max_pages: int) -> PdfInfo:
    if not data:
        raise PdfValidationError("empty_file", "The uploaded file is empty.")
    if len(data) > max_bytes:
        raise PdfValidationError(
            "file_too_large",
            f"File is {len(data) / (1024 * 1024):.1f} MB; the limit is {max_bytes // (1024 * 1024)} MB.",
        )
    # A PDF header may be preceded by a little junk, but must appear near the start.
    if b"%PDF-" not in data[:1024]:
        raise PdfValidationError("not_a_pdf", "The file does not look like a PDF (missing %PDF header).")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise PdfValidationError("malformed_pdf", f"The PDF could not be opened: {exc}") from exc

    try:
        if not doc.is_pdf:
            raise PdfValidationError("not_a_pdf", "The file is not a PDF document.")
        if doc.is_encrypted or doc.needs_pass:
            raise PdfValidationError(
                "encrypted_pdf",
                "The PDF is password protected. Remove the password and upload again.",
            )
        page_count = doc.page_count
        if page_count == 0:
            raise PdfValidationError("no_pages", "The PDF contains no pages.")
        if page_count > max_pages:
            raise PdfValidationError(
                "too_many_pages", f"The PDF has {page_count} pages; the limit is {max_pages}."
            )
        return PdfInfo(page_count=page_count)
    finally:
        doc.close()


@dataclass
class ExtractedPage:
    page_number: int
    geometry: PageGeometry
    kind: PageKind
    text: str
    words: list[Word]
    image_count: int
    image_coverage: float

    @property
    def supported(self) -> bool:
        return self.kind == "text"

    @property
    def word_count(self) -> int:
        return len(self.words)


@dataclass
class ExtractionResult:
    pages: list[ExtractedPage]
    info: ExtractionInfo
    limitations: list[str] = field(default_factory=list)

    @property
    def all_supported(self) -> bool:
        return all(p.supported for p in self.pages)


def _page_geometry(page: pymupdf.Page) -> PageGeometry:
    # page.rect is the *rotated* rectangle; words are reported in unrotated space.
    rotation = page.rotation % 360
    width, height = page.rect.width, page.rect.height
    if rotation in (90, 270):
        width, height = height, width
    return PageGeometry(width=width, height=height, rotation=rotation)


def _image_coverage(page: pymupdf.Page) -> tuple[int, float]:
    """Return (image_count, fraction_of_page_area_covered_by_images)."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0, 0.0
    images = page.get_images(full=True)
    covered = 0.0
    for img in images:
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:  # pragma: no cover - defensive against odd PDFs
            continue
        for r in rects:
            clipped = r & page.rect  # intersect with the visible page
            if not clipped.is_empty:
                covered += clipped.width * clipped.height
    # Overlapping images can exceed the page; cap at 1.0 for a clean fraction.
    return len(images), min(covered / page_area, 1.0)


def _classify(word_count: int, image_coverage: float) -> PageKind:
    if word_count == 0:
        return "scanned" if image_coverage >= SCANNED_IMAGE_COVERAGE else "empty"
    if image_coverage >= MIXED_IMAGE_COVERAGE:
        return "mixed"
    return "text"


def _extract_words(page: pymupdf.Page) -> tuple[str, list[Word]]:
    """Build page text from words so character offsets map exactly onto rectangles.

    Words in the same line are joined by a single space; lines by a newline.
    ``sort=True`` orders blocks top-to-bottom, left-to-right, which gives a
    stable reading order for ordinary single-column documents.
    """
    raw = page.get_text("words", flags=_WORD_FLAGS, sort=True)
    words: list[Word] = []
    parts: list[str] = []
    cursor = 0
    prev_key: tuple[int, int] | None = None
    for x0, y0, x1, y1, text, block, line, wno in raw:
        text = text.strip()
        if not text:
            continue
        key = (block, line)
        if prev_key is not None:
            sep = " " if key == prev_key else "\n"
            parts.append(sep)
            cursor += 1
        start = cursor
        parts.append(text)
        cursor += len(text)
        words.append(
            Word(
                text=text,
                bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                block=block,
                line=line,
                word_index=wno,
                char_start=start,
                char_end=cursor,
            )
        )
        prev_key = key
    return "".join(parts), words


def extract_document(data: bytes) -> ExtractionResult:
    """Extract every page. Caller is responsible for having validated the bytes."""
    started = time.perf_counter()
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages: list[ExtractedPage] = []
    limitations: list[str] = []
    try:
        for index, page in enumerate(doc):
            text, words = _extract_words(page)
            image_count, coverage = _image_coverage(page)
            kind = _classify(len(words), coverage)
            pages.append(
                ExtractedPage(
                    page_number=index + 1,
                    geometry=_page_geometry(page),
                    kind=kind,
                    text=text,
                    words=words,
                    image_count=image_count,
                    image_coverage=round(coverage, 4),
                )
            )
    finally:
        doc.close()

    unsupported = [p.page_number for p in pages if not p.supported]
    if unsupported:
        kinds = sorted({p.kind for p in pages if not p.supported})
        limitations.append(
            f"{len(unsupported)} page(s) are {'/'.join(kinds)} and cannot be analysed as text. "
            "OCR is not implemented; content in images is not searched or redacted."
        )
    limitations.append("Text inside images, annotations and form fields is not extracted.")

    info = ExtractionInfo(
        engine=ENGINE_NAME,
        engine_version=ENGINE_VERSION,
        duration_ms=int((time.perf_counter() - started) * 1000),
        supported_pages=len(pages) - len(unsupported),
        unsupported_pages=unsupported,
        limitations=limitations,
    )
    return ExtractionResult(pages=pages, info=info, limitations=limitations)
