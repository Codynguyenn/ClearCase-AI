"""Build a redacted PDF and independently verify it.

What "redaction" means here
---------------------------
For every approved rectangle we use PyMuPDF's *real* redaction (``apply_redactions``),
which edits the page's content stream: text under the box is deleted (not covered),
image pixels under the box are blanked, and vector graphics touching the box are
removed. A black box is then drawn in its place. Afterwards we:

* delete every annotation and form widget (they can hold copies of sensitive text),
* scrub metadata, XMP, attachments, thumbnails, JavaScript, links,
* clear bookmarks and page labels,
* rewrite the whole file (``garbage=4``) so no earlier revision of the file survives.

What we do NOT promise (and say so in the scope statement)
----------------------------------------------------------
* Text that lives only inside an image is not searched; image *pixels* under approved
  boxes are blanked, but sensitive text elsewhere in an image is untouched.
* Unapproved text is left alone, even if it looks like approved text.
* Files with optional-content layers or XFA forms are refused: we cannot reliably
  remove content that may be hidden in a layer or a separate XML form.

Verification never trusts the build step: it re-opens the output bytes and checks.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pymupdf

from app.models.common import BBox
from app.models.findings import RetainedCopies, VerificationCheck
from app.services.coverage import any_overlap, boxes_overlap, find_copies
from app.services.pdf_processing import extract_document

BLACK_THRESHOLD = 12  # 0-255 grey level that still counts as "black"
BLACK_FRACTION_REQUIRED = 0.98
RENDER_SCALE = 2.0
INSET = 0.75  # points: ignore anti-aliased edges when sampling pixels

BoxesByPage = Mapping[int, Sequence[BBox]]


class ExportBlocked(Exception):
    """The document cannot be sanitised reliably; the message is shown to the reviewer."""

    def __init__(self, message: str, code: str = "cannot_sanitize") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _rect(b: BBox) -> pymupdf.Rect:
    return pymupdf.Rect(b.x0, b.y0, b.x1, b.y1)


# ------------------------------------------------------------------------ build


def check_sanitizable(doc: pymupdf.Document) -> None:
    if doc.needs_pass or doc.is_encrypted:
        raise ExportBlocked("The PDF is encrypted; ClearCase cannot redact it.", code="encrypted")
    try:
        if doc.get_ocgs():
            raise ExportBlocked(
                "The PDF has optional-content layers. Content may be hidden in a layer, "
                "so removal cannot be guaranteed. Flatten the PDF first.",
                code="has_layers",
            )
    except ExportBlocked:
        raise
    except Exception:  # noqa: BLE001 - treat inspection failure as unsafe
        raise ExportBlocked("Could not inspect optional-content layers; refusing to export.") from None
    catalog = doc.pdf_catalog()
    if catalog and doc.xref_get_key(catalog, "AcroForm/XFA")[0] != "null":
        raise ExportBlocked("The PDF contains an XFA form, which cannot be sanitised reliably.", code="has_xfa")


def build_redacted_pdf(original: bytes, boxes_by_page: BoxesByPage) -> bytes:
    doc = pymupdf.open(stream=original, filetype="pdf")
    try:
        check_sanitizable(doc)
        for page_number, boxes in sorted(boxes_by_page.items()):
            if not boxes:
                continue
            page = doc[page_number - 1]
            for b in boxes:
                page.add_redact_annot(_rect(b), fill=(0, 0, 0))
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

        for page in doc:
            for widget in list(page.widgets() or []):
                page.delete_widget(widget)
            for annot in list(page.annots() or []):
                page.delete_annot(annot)

        doc.scrub(
            metadata=True,
            xml_metadata=True,
            attached_files=True,
            embedded_files=True,
            thumbnails=True,
            javascript=True,
            hidden_text=False,  # we already removed approved text; do not mutate other text
            redactions=True,
            redact_images=0,
            remove_links=True,
            reset_fields=True,
            reset_responses=True,
            clean_pages=True,
        )
        doc.set_metadata({})
        doc.set_toc([])
        try:
            doc.set_page_labels([])
        except Exception:  # noqa: BLE001 - not every PDF has labels
            pass
        # A full (non-incremental) rewrite with garbage collection drops unreferenced
        # objects, including old revisions of redacted content.
        return doc.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()


# ----------------------------------------------------------------------- verify


def _unrotated_to_render(page: pymupdf.Page, rect: pymupdf.Rect) -> pymupdf.Rect:
    """Boxes are stored in unrotated page space; ``get_pixmap`` clips in rotated space."""
    return (rect * page.rotation_matrix).normalize()


def _black_fraction(page: pymupdf.Page, box: BBox) -> float:
    rect = _rect(box)
    inner = pymupdf.Rect(rect.x0 + INSET, rect.y0 + INSET, rect.x1 - INSET, rect.y1 - INSET)
    if inner.is_empty or inner.width < 0.5 or inner.height < 0.5:
        inner = rect  # tiny box: sample everything
    clip = _unrotated_to_render(page, inner) & page.rect
    if clip.is_empty:
        return 0.0
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE), clip=clip, colorspace=pymupdf.csGRAY, alpha=False
    )
    samples = pix.samples
    if not samples:
        return 0.0
    dark = sum(1 for v in samples if v <= BLACK_THRESHOLD)
    return dark / len(samples)


def _words(page: pymupdf.Page) -> list[tuple[BBox, str]]:
    return [
        (BBox(x0=w[0], y0=w[1], x1=w[2], y1=w[3]), w[4])
        for w in page.get_text("words", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
        if w[4].strip()
    ]


def _fmt_pages(pages: Sequence[int]) -> str:
    return ", ".join(str(p) for p in sorted(set(pages))) or "none"


def verify_redaction(
    *,
    original: bytes,
    output: bytes,
    expected_original_hash: str,
    original_hash_now: str,
    approved: BoxesByPage,
    rejected: BoxesByPage,
    approved_texts: Sequence[str],
) -> tuple[list[VerificationCheck], RetainedCopies]:
    """Run every check against the output bytes. Never raises for a failed check."""
    checks: list[VerificationCheck] = []
    retained = RetainedCopies()

    # 1. Output reopens and has the same page count.
    try:
        out = pymupdf.open(stream=output, filetype="pdf")
        src = pymupdf.open(stream=original, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        checks.append(VerificationCheck(name="output_opens", passed=False, detail=f"Output could not be opened: {type(exc).__name__}"))
        return checks, retained
    try:
        ok = out.page_count == src.page_count and not out.needs_pass
        checks.append(
            VerificationCheck(
                name="output_opens",
                passed=ok,
                detail=f"Output reopens with {out.page_count} page(s) (original {src.page_count}).",
            )
        )

        # 2. No text remains inside approved boxes.
        leaking: list[int] = []
        for pn, boxes in approved.items():
            words = _words(out[pn - 1])
            if any(boxes_overlap(wb, b) for wb, _ in words for b in boxes):
                leaking.append(pn)
        checks.append(
            VerificationCheck(
                name="text_removed_in_approved_boxes",
                passed=not leaking,
                detail="No extractable text inside approved boxes."
                if not leaking
                else f"Text still present inside approved boxes on page(s) {_fmt_pages(leaking)}.",
            )
        )

        # 3. Approved boxes render solid black.
        not_black: list[int] = []
        for pn, boxes in approved.items():
            page = out[pn - 1]
            if any(_black_fraction(page, b) < BLACK_FRACTION_REQUIRED for b in boxes):
                not_black.append(pn)
        checks.append(
            VerificationCheck(
                name="boxes_render_black",
                passed=not not_black,
                detail="Every approved box renders solid black."
                if not not_black
                else f"Some approved boxes are not solid black on page(s) {_fmt_pages(not_black)}.",
            )
        )

        # 4. Metadata, attachments, annotations, bookmarks scrubbed.
        problems: list[str] = []
        meta = {k: v for k, v in (out.metadata or {}).items() if k not in ("format", "encryption") and v}
        if meta:
            problems.append(f"metadata fields remain: {', '.join(sorted(meta))}")
        if out.embfile_count():
            problems.append("embedded files remain")
        if out.xref_xml_metadata():
            problems.append("XMP metadata remains")
        if out.get_toc():
            problems.append("bookmarks remain")
        annots = sum(len(list(p.annots() or [])) + len(list(p.widgets() or [])) for p in out)
        if annots:
            problems.append(f"{annots} annotation/form object(s) remain")
        checks.append(
            VerificationCheck(
                name="metadata_and_attachments_scrubbed",
                passed=not problems,
                detail="Metadata, XMP, attachments, annotations, form fields and bookmarks removed."
                if not problems
                else "; ".join(problems),
            )
        )

        # 5. Full rewrite (no earlier file revisions appended).
        eof_count = len(re.findall(rb"%%EOF", output))
        checks.append(
            VerificationCheck(
                name="full_rewrite",
                passed=eof_count == 1,
                detail="File was fully rewritten (single end-of-file marker)."
                if eof_count == 1
                else f"Found {eof_count} end-of-file markers; earlier revisions may remain.",
            )
        )

        # 6. Original unchanged.
        unchanged = original_hash_now == expected_original_hash and hashlib.sha256(original).hexdigest() == expected_original_hash
        checks.append(
            VerificationCheck(
                name="original_unchanged",
                passed=unchanged,
                detail="Stored original still matches its upload hash."
                if unchanged
                else "Stored original no longer matches its upload hash.",
            )
        )

        # 7. Collateral: words outside approved boxes should survive (informational).
        lost_pages: dict[int, int] = {}
        for pn in range(1, src.page_count + 1):
            boxes = list(approved.get(pn, []))
            before = Counter(t for wb, t in _words(src[pn - 1]) if not any(boxes_overlap(wb, b) for b in boxes))
            after = Counter(t for _, t in _words(out[pn - 1]))
            missing = sum((before - after).values())
            if missing:
                lost_pages[pn] = missing
        total_lost = sum(lost_pages.values())
        checks.append(
            VerificationCheck(
                name="no_collateral_text_loss",
                passed=total_lost == 0,
                required=False,
                detail="All text outside approved boxes is intact."
                if not total_lost
                else f"{total_lost} word(s) outside approved boxes are missing on page(s) {_fmt_pages(list(lost_pages))}; "
                "they may have touched an approved box. Review the output.",
            )
        )
    finally:
        out.close()
        src.close()

    # 8. Where else does approved text still appear in the output?
    retained = classify_remaining_copies(output, approved_texts, rejected)
    checks.append(
        VerificationCheck(
            name="no_unresolved_copies",
            passed=retained.unresolved == 0,
            required=False,
            detail=(
                "No remaining copies of approved text outside locations you chose to keep."
                if retained.unresolved == 0
                else f"{retained.unresolved} remaining copy(ies) of approved text on page(s) "
                f"{_fmt_pages([int(loc['page']) for loc in retained.unresolved_locations])} are not covered by any finding."
            )
            + (f" {retained.intentionally_retained} kept on purpose (rejected findings)." if retained.intentionally_retained else ""),
        )
    )
    return checks, retained


def classify_remaining_copies(output: bytes, approved_texts: Sequence[str], rejected: BoxesByPage) -> RetainedCopies:
    texts = sorted({t for t in approved_texts if t.strip()})
    if not texts:
        return RetainedCopies()
    pages = extract_document(output).pages
    result = RetainedCopies()
    for copy in find_copies(pages, texts):
        if any_overlap(copy.boxes, rejected.get(copy.page_number, [])):
            result.intentionally_retained += 1
        else:
            result.unresolved += 1
            result.unresolved_locations.append({"page": copy.page_number, "group": copy.text_index})
    return result
