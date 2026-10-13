"""Unit tests for validation and extraction (no database)."""

from __future__ import annotations

import pytest

from app.services.pdf_processing import PdfValidationError, extract_document, validate_pdf
from tests.conftest import make_encrypted_pdf, make_mixed_pdf, make_scanned_pdf, make_text_pdf

LIMITS = {"max_bytes": 1024 * 1024, "max_pages": 10}


class TestValidate:
    def test_accepts_text_pdf(self):
        info = validate_pdf(make_text_pdf([["hello"], ["world"]]), **LIMITS)
        assert info.page_count == 2

    @pytest.mark.parametrize(
        ("data", "code"),
        [
            (b"", "empty_file"),
            (b"this is plain text, not a pdf", "not_a_pdf"),
            (b"%PDF-1.7 garbage garbage garbage", "malformed_pdf"),
        ],
    )
    def test_rejects_non_pdfs(self, data: bytes, code: str):
        with pytest.raises(PdfValidationError) as exc:
            validate_pdf(data, **LIMITS)
        assert exc.value.code == code

    def test_rejects_encrypted(self):
        with pytest.raises(PdfValidationError) as exc:
            validate_pdf(make_encrypted_pdf(), **LIMITS)
        assert exc.value.code == "encrypted_pdf"

    def test_rejects_too_large(self):
        data = make_text_pdf([["x"]])
        with pytest.raises(PdfValidationError) as exc:
            validate_pdf(data, max_bytes=len(data) - 1, max_pages=10)
        assert exc.value.code == "file_too_large"

    def test_rejects_too_many_pages(self):
        data = make_text_pdf([["p"]] * 3)
        with pytest.raises(PdfValidationError) as exc:
            validate_pdf(data, max_bytes=10**6, max_pages=2)
        assert exc.value.code == "too_many_pages"


class TestExtract:
    def test_words_map_back_to_text_offsets(self):
        result = extract_document(make_text_pdf([["Alpha Beta", "Gamma"]]))
        page = result.pages[0]
        assert page.kind == "text"
        assert page.text == "Alpha Beta\nGamma"
        for word in page.words:
            # The span->rectangle map must be exact: slicing the text by the
            # stored offsets must give back the word itself.
            assert page.text[word.char_start : word.char_end] == word.text
            assert word.bbox.x1 > word.bbox.x0 and word.bbox.y1 > word.bbox.y0

    def test_unicode_and_punctuation_preserved(self):
        result = extract_document(make_text_pdf([["José Álvarez, Zoë (née Müller)"]]))
        page = result.pages[0]
        assert "José" in page.text and "Álvarez," in page.text and "Zoë" in page.text
        texts = [w.text for w in page.words]
        assert "Álvarez," in texts

    def test_geometry_reports_unrotated_size_and_rotation(self):
        result = extract_document(make_text_pdf([["rotated"]], rotation=90))
        geom = result.pages[0].geometry
        assert geom.rotation == 90
        # Page was created 400x500; the unrotated size must be preserved even
        # though the rotated page.rect would be 500x400.
        assert (geom.width, geom.height) == (400, 500)
        word = result.pages[0].words[0]
        assert 0 <= word.bbox.x0 <= 400 and 0 <= word.bbox.y0 <= 500

    def test_scanned_page_is_flagged_unsupported(self):
        result = extract_document(make_scanned_pdf(pages=2))
        assert [p.kind for p in result.pages] == ["scanned", "scanned"]
        assert not result.all_supported
        assert result.info.unsupported_pages == [1, 2]
        assert result.info.supported_pages == 0
        assert any("OCR" in note for note in result.info.limitations)

    def test_mixed_page_is_flagged(self):
        result = extract_document(make_mixed_pdf())
        page = result.pages[0]
        assert page.kind == "mixed"
        assert page.word_count > 0
        assert page.image_coverage >= 0.3
        assert not page.supported

    def test_mixed_document_keeps_text_pages_supported(self):
        import pymupdf

        text_part = pymupdf.open("pdf", make_text_pdf([["readable page"]]))
        scan_part = pymupdf.open("pdf", make_scanned_pdf())
        text_part.insert_pdf(scan_part)
        result = extract_document(text_part.tobytes())
        assert [p.kind for p in result.pages] == ["text", "scanned"]
        assert result.info.supported_pages == 1
        assert result.info.unsupported_pages == [2]
