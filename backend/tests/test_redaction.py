"""Redaction engine tests: the output PDF is inspected directly with PyMuPDF."""

from __future__ import annotations

import hashlib

import pymupdf
import pytest

from app.models.common import BBox
from app.services.pdf_processing import extract_document
from app.services.redaction import ExportBlocked, build_redacted_pdf, verify_redaction

SECRET = "Maria Delgado"
KEEP = "Quarterly Report"


def boxes_for(data: bytes, needle: str) -> dict[int, list[BBox]]:
    """Word-box rectangles for every occurrence of ``needle`` (using our own extraction)."""
    result: dict[int, list[BBox]] = {}
    first, *rest = needle.split()
    for page in extract_document(data).pages:
        words = page.words
        for i, w in enumerate(words):
            if w.text == first and [x.text for x in words[i + 1 : i + 1 + len(rest)]] == rest:
                for x in words[i : i + 1 + len(rest)]:
                    result.setdefault(page.page_number, []).append(x.bbox)
    return result


def page_text(data: bytes, page_number: int = 1) -> str:
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return doc[page_number - 1].get_text()
    finally:
        doc.close()


def simple_pdf(*, rotation: int = 0, cropbox: pymupdf.Rect | None = None, extras=None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((60, 100), f"{KEEP} for {SECRET} only.", fontsize=12)
    page.insert_text((60, 130), "Nothing else sensitive here.", fontsize=12)
    if extras:
        extras(doc, page)
    if cropbox is not None:
        page.set_cropbox(cropbox)
    if rotation:
        page.set_rotation(rotation)
    doc.set_metadata({"title": "Secret title Maria", "author": "Maria Delgado"})
    data = doc.tobytes()
    doc.close()
    return data


def run(original: bytes, needle: str = SECRET, rejected=None, texts=None):
    approved = boxes_for(original, needle)
    out = build_redacted_pdf(original, approved)
    h = hashlib.sha256(original).hexdigest()
    checks, retained = verify_redaction(
        original=original, output=out, expected_original_hash=h, original_hash_now=h,
        approved=approved, rejected=rejected or {}, approved_texts=texts if texts is not None else [needle],
    )
    return out, {c.name: c for c in checks}, retained


def assert_all_required_pass(checks):
    failed = [c for c in checks.values() if c.required and not c.passed]
    assert not failed, [(c.name, c.detail) for c in failed]


def test_text_is_actually_removed_not_covered():
    out, checks, retained = run(simple_pdf())
    assert_all_required_pass(checks)
    text = page_text(out)
    assert "Maria" not in text and "Delgado" not in text  # gone from the content stream
    assert KEEP in text and "Nothing else sensitive here." in text  # everything else intact
    assert b"Maria" not in out and b"Delgado" not in out
    assert checks["no_collateral_text_loss"].passed and retained.unresolved == 0


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_pages_are_redacted_in_the_right_place(rotation: int):
    out, checks, _ = run(simple_pdf(rotation=rotation))
    assert_all_required_pass(checks)
    text = page_text(out)
    assert "Delgado" not in text and KEEP in text


def test_cropped_page_with_offset_origin():
    out, checks, _ = run(simple_pdf(cropbox=pymupdf.Rect(30, 60, 380, 300)))
    assert_all_required_pass(checks)
    assert "Delgado" not in page_text(out) and KEEP in page_text(out)


def test_metadata_annotations_and_attachments_are_scrubbed():
    def extras(doc, page):
        page.add_text_annot((200, 200), f"note about {SECRET}")
        doc.embfile_add("secret.txt", b"Maria Delgado ssn 512-44-9087")
        doc.set_toc([[1, f"Chapter {SECRET}", 1]])

    out, checks, _ = run(simple_pdf(extras=extras))
    assert_all_required_pass(checks)
    doc = pymupdf.open(stream=out, filetype="pdf")
    try:
        assert not any(v for k, v in doc.metadata.items() if k not in ("format", "encryption"))
        assert doc.embfile_count() == 0 and doc.get_toc() == []
        assert list(doc[0].annots() or []) == []
    finally:
        doc.close()
    assert b"Delgado" not in out and b"512-44-9087" not in out


def test_image_pixels_under_the_box_are_blanked():
    def extras(doc, page):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(255)  # a white square
        page.insert_image(pymupdf.Rect(60, 200, 160, 300), pixmap=pix)

    original = simple_pdf(extras=extras)
    approved = {1: [BBox(x0=70, y0=210, x1=150, y1=290)]}
    out = build_redacted_pdf(original, approved)
    doc = pymupdf.open(stream=out, filetype="pdf")
    try:
        pix = doc[0].get_pixmap(clip=pymupdf.Rect(75, 215, 145, 285), colorspace=pymupdf.csGRAY)
        assert max(pix.samples) <= 12  # was pure white before redaction
        # Away from the box the image is still white. (Pixel redaction blanks whole *source*
        # pixels that touch the box - here 2.5pt each - so it may over-redact by that much,
        # never under-redact. We sample beyond that margin.)
        outside = doc[0].get_pixmap(clip=pymupdf.Rect(60.5, 215, 64, 285), colorspace=pymupdf.csGRAY)
        assert min(outside.samples) >= 200
    finally:
        doc.close()


def test_vector_graphics_under_the_box_are_removed():
    def extras(doc, page):
        shape = page.new_shape()
        shape.draw_rect(pymupdf.Rect(60, 200, 160, 260))
        shape.finish(fill=(1, 0, 0), color=(1, 0, 0))
        shape.commit()

    original = simple_pdf(extras=extras)
    out = build_redacted_pdf(original, {1: [BBox(x0=55, y0=195, x1=170, y1=270)]})
    doc = pymupdf.open(stream=out, filetype="pdf")
    try:
        pix = doc[0].get_pixmap(clip=pymupdf.Rect(60, 200, 160, 260), colorspace=pymupdf.csRGB)
        reds = sum(1 for i in range(0, len(pix.samples), 3) if pix.samples[i] > 200 and pix.samples[i + 1] < 50)
        assert reds == 0
    finally:
        doc.close()


def test_rejected_location_is_reported_as_intentionally_retained():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_text((60, 100), f"{SECRET} is the signer.", fontsize=12)
    page.insert_text((60, 130), f"cc: {SECRET} (kept on purpose)", fontsize=12)
    page.insert_text((60, 160), f"also {SECRET} here unreviewed", fontsize=12)
    original = doc.tobytes()
    doc.close()

    every = boxes_for(original, SECRET)[1]
    approved = {1: [every[0], every[1]]}  # first occurrence (2 words)
    rejected = {1: [every[2], every[3]]}  # second occurrence
    out = build_redacted_pdf(original, approved)
    h = hashlib.sha256(original).hexdigest()
    checks, retained = verify_redaction(
        original=original, output=out, expected_original_hash=h, original_hash_now=h,
        approved=approved, rejected=rejected, approved_texts=[SECRET],
    )
    assert retained.intentionally_retained == 1
    assert retained.unresolved == 1 and retained.unresolved_locations == [{"page": 1, "group": 0}]
    by_name = {c.name: c for c in checks}
    assert by_name["no_unresolved_copies"].passed is False and by_name["no_unresolved_copies"].required is False
    assert "Delgado" not in str(retained)  # no text in the report


def _with_comments(doc: pymupdf.Document, page: pymupdf.Page) -> None:
    """A reviewer's margin comment and a filled-in form field: both are removed by the export on purpose."""
    page.add_freetext_annot(pymupdf.Rect(60, 200, 360, 240), "Free text: reviewer comment here")
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "note"
    widget.field_value = "Filled in value"
    widget.rect = pymupdf.Rect(60, 260, 300, 290)
    page.add_widget(widget)


def test_removed_annotations_and_form_fields_are_not_reported_as_lost_text():
    """Regression: words inside annotations/form fields used to be counted as 'lost', a false alarm."""
    original = simple_pdf(extras=_with_comments)
    assert "reviewer comment" in page_text(original)  # the comment text really is part of the extraction
    out, checks, _ = run(original)
    assert_all_required_pass(checks)
    assert checks["no_collateral_text_loss"].passed is True
    assert "reviewer comment" not in page_text(out)  # ...and it really was removed from the output


def test_real_loss_of_unapproved_text_is_still_reported():
    original = simple_pdf(extras=_with_comments)
    approved = boxes_for(original, SECRET)
    # An output that silently lost a sentence nobody approved removing.
    damaged_doc = pymupdf.open()
    page = damaged_doc.new_page(width=400, height=500)
    page.insert_text((60, 100), f"{KEEP} for [removed] only.", fontsize=12)  # "Nothing else sensitive here." is gone
    damaged = damaged_doc.tobytes()
    h = hashlib.sha256(original).hexdigest()
    checks, _ = verify_redaction(
        original=original, output=damaged, expected_original_hash=h, original_hash_now=h,
        approved=approved, rejected={}, approved_texts=[SECRET],
    )
    collateral = {c.name: c for c in checks}["no_collateral_text_loss"]
    assert collateral.passed is False and collateral.required is False
    assert "missing" in collateral.detail


def test_verification_catches_a_failed_redaction():
    original = simple_pdf()
    approved = boxes_for(original, SECRET)
    h = hashlib.sha256(original).hexdigest()
    # Pretend the "output" is just the untouched original: text leaks, boxes are not black.
    checks, _ = verify_redaction(
        original=original, output=original, expected_original_hash=h, original_hash_now=h,
        approved=approved, rejected={}, approved_texts=[SECRET],
    )
    by_name = {c.name: c for c in checks}
    assert by_name["text_removed_in_approved_boxes"].passed is False
    assert by_name["boxes_render_black"].passed is False
    assert by_name["metadata_and_attachments_scrubbed"].passed is False


def test_verification_detects_changed_original_and_incremental_save():
    original = simple_pdf()
    approved = boxes_for(original, SECRET)
    out = build_redacted_pdf(original, approved)
    checks, _ = verify_redaction(
        original=original, output=out, expected_original_hash="0" * 64, original_hash_now="1" * 64,
        approved=approved, rejected={}, approved_texts=[],
    )
    assert {c.name: c for c in checks}["original_unchanged"].passed is False

    appended = out + b"\n%%EOF\n"
    checks, _ = verify_redaction(
        original=original, output=appended, expected_original_hash=hashlib.sha256(original).hexdigest(),
        original_hash_now=hashlib.sha256(original).hexdigest(), approved=approved, rejected={}, approved_texts=[],
    )
    assert {c.name: c for c in checks}["full_rewrite"].passed is False


def test_layers_are_refused():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), SECRET)
    doc.add_ocg("Hidden layer", on=False)
    data = doc.tobytes()
    doc.close()
    with pytest.raises(ExportBlocked) as exc:
        build_redacted_pdf(data, {1: [BBox(x0=40, y0=30, x1=150, y1=60)]})
    assert exc.value.code == "has_layers"


def test_scanned_page_manual_box_blanks_pixels():
    from tests.conftest import make_scanned_pdf

    original = make_scanned_pdf()
    approved = {1: [BBox(x0=50, y0=50, x1=200, y1=120)]}
    out = build_redacted_pdf(original, approved)
    h = hashlib.sha256(original).hexdigest()
    checks, _ = verify_redaction(
        original=original, output=out, expected_original_hash=h, original_hash_now=h,
        approved=approved, rejected={}, approved_texts=[],
    )
    assert_all_required_pass({c.name: c for c in checks})
