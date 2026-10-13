"""Search service tests.

Expected counts are derived by reading `sample_data/generate_samples.py`, not by
running the code under test. The sample contract contains:

* "Maria Delgado-Reyes" 5 times: page 1 x3, page 2 x1 (wrapped over two lines:
  "Maria" / "Delgado-Reyes"), page 3 x1.
* "José Álvarez" 4 times: page 1 x2, page 2 x1 (followed by a comma), page 3 x1.
* The lower-case string "maria" once inside the e-mail address maria.delgado@...
* The SSN-format value "512-44-9087" once on page 2, directly followed by a period.
"""

from __future__ import annotations

import pytest
import pymupdf

from app.services.errors import ValidationFailed
from app.services.pdf_processing import extract_document
from app.services.search import MAX_QUERY_CHARS, make_occurrence_id, search_pages
from tests.conftest import SAMPLE_CONTRACT, make_scanned_pdf, make_text_pdf


@pytest.fixture(scope="module")
def sample_pages():
    return extract_document(SAMPLE_CONTRACT.read_bytes()).pages


def run(pages, query, **kw):
    return search_pages(pages, query, document_id="doc1", document_version=1, **kw)


class TestRepeatedOccurrences:
    def test_finds_every_occurrence_of_a_name(self, sample_pages):
        result = run(sample_pages, "Maria Delgado-Reyes")
        assert result.total == 5
        assert [o.page_number for o in result.occurrences] == [1, 1, 1, 2, 3]

    def test_occurrence_text_equals_slice_of_page_text(self, sample_pages):
        by_page = {p.page_number: p for p in sample_pages}
        for occ in run(sample_pages, "Maria Delgado-Reyes").occurrences:
            page = by_page[occ.page_number]
            assert page.text[occ.char_start : occ.char_end].split() == ["Maria", "Delgado-Reyes"]

    def test_ids_are_unique_and_stable(self, sample_pages):
        a = run(sample_pages, "Maria Delgado-Reyes")
        b = run(sample_pages, "Maria Delgado-Reyes")
        ids = [o.occurrence_id for o in a.occurrences]
        assert len(set(ids)) == 5
        assert ids == [o.occurrence_id for o in b.occurrences]

    def test_id_changes_with_document_version(self):
        assert make_occurrence_id("d", 1, 1, 0, 5) != make_occurrence_id("d", 2, 1, 0, 5)


class TestMultiLine:
    def test_wrapped_name_matches_and_has_one_box_per_line(self, sample_pages):
        wrapped = [o for o in run(sample_pages, "Maria Delgado-Reyes").occurrences if o.page_number == 2]
        assert len(wrapped) == 1
        occ = wrapped[0]
        assert len(occ.boxes) == 2
        first, second = occ.boxes
        assert second.y0 > first.y1 - 1  # second box is on the next line
        assert occ.mappable and not occ.partial_word

    def test_single_line_match_has_one_box(self, sample_pages):
        page1 = [o for o in run(sample_pages, "Maria Delgado-Reyes").occurrences if o.page_number == 3]
        assert len(page1[0].boxes) == 1


class TestCaseAndWholeWord:
    def test_case_insensitive_by_default(self, sample_pages):
        assert run(sample_pages, "maria").total == 6  # 5 names + the e-mail local part

    def test_case_sensitive_option(self, sample_pages):
        result = run(sample_pages, "maria", case_sensitive=True)
        assert result.total == 1
        assert "@" in result.occurrences[0].context

    def test_substring_vs_whole_word(self, sample_pages):
        assert run(sample_pages, "Mari").total == 6
        assert run(sample_pages, "Mari", whole_word=True).total == 0
        assert run(sample_pages, "Maria", whole_word=True).total == 6

    def test_whole_word_on_query_that_starts_with_punctuation(self, sample_pages):
        # "(206)" begins with a non-word character; boundary guard must not block it.
        assert run(sample_pages, "(206) 555-0142", whole_word=True).total == 1


class TestUnicodeAndPunctuation:
    def test_accented_name(self, sample_pages):
        assert run(sample_pages, "José Álvarez").total == 4

    def test_accent_sensitive(self, sample_pages):
        # Without accents this is a different string, so it must not match.
        assert run(sample_pages, "Jose Alvarez").total == 0

    def test_decomposed_query_matches_composed_text(self, sample_pages):
        decomposed = "Jose\u0301 A\u0301lvarez"  # e + combining acute, A + combining acute
        assert run(sample_pages, decomposed).total == 4

    def test_en_dash_in_query_matches_hyphen_in_text(self, sample_pages):
        assert run(sample_pages, "Delgado\u2013Reyes").total == 5

    def test_extra_whitespace_in_query_is_tolerated(self, sample_pages):
        assert run(sample_pages, "  Maria    Delgado-Reyes ").total == 5

    def test_curly_apostrophe_matches_straight(self):
        pages = extract_document(make_text_pdf([["The party's duty"]])).pages
        assert run(pages, "party\u2019s duty").total == 1


class TestSpansAndBoxes:
    def test_partial_word_match_is_flagged_and_box_covers_whole_word(self, sample_pages):
        occ = run(sample_pages, "512-44-9087").occurrences[0]
        page = next(p for p in sample_pages if p.page_number == occ.page_number)
        assert occ.partial_word  # the word is "512-44-9087." with a trailing period
        assert page.text[occ.char_start : occ.char_end] == "512-44-9087"
        word = next(w for w in page.words if w.text.startswith("512-44-9087"))
        box = occ.boxes[0]
        assert (box.x0, box.x1) == (word.bbox.x0, word.bbox.x1)

    def test_boxes_lie_inside_the_page(self, sample_pages):
        for occ in run(sample_pages, "Álvarez").occurrences:
            page = next(p for p in sample_pages if p.page_number == occ.page_number)
            for b in occ.boxes:
                assert 0 <= b.x0 < b.x1 <= page.geometry.width
                assert 0 <= b.y0 < b.y1 <= page.geometry.height

    def test_rotated_page_boxes_are_in_unrotated_space(self):
        pages = extract_document(make_text_pdf([["Rotated Target here"]], rotation=90)).pages
        occ = run(pages, "Target").occurrences[0]
        g = pages[0].geometry
        assert g.rotation == 90 and (g.width, g.height) == (400, 500)
        b = occ.boxes[0]
        assert 0 <= b.x0 < b.x1 <= g.width and 0 <= b.y0 < b.y1 <= g.height


class TestLimitsAndCoverage:
    def test_empty_and_blank_queries_rejected(self, sample_pages):
        for q in ("", "   ", "\n\t"):
            with pytest.raises(ValidationFailed) as exc:
                run(sample_pages, q)
            assert exc.value.code == "empty_query"

    def test_overlong_query_rejected(self, sample_pages):
        with pytest.raises(ValidationFailed) as exc:
            run(sample_pages, "a" * (MAX_QUERY_CHARS + 1))
        assert exc.value.code == "query_too_long"

    def test_regex_metacharacters_are_literal(self, sample_pages):
        # If this were treated as a regex ".*" would match everything.
        assert run(sample_pages, ".*").total == 0
        assert run(sample_pages, "(a+)+$").total == 0

    def test_result_cap_is_reported(self, sample_pages):
        result = run(sample_pages, "a", max_results=3)
        assert result.total == 3 and result.truncated
        assert any("capped" in n for n in result.notes)

    def test_unsupported_pages_are_reported_not_hidden(self):
        doc = pymupdf.open("pdf", make_text_pdf([["Findable text"]]))
        doc.insert_pdf(pymupdf.open("pdf", make_scanned_pdf()))
        pages = extract_document(doc.tobytes()).pages
        result = run(pages, "Findable")
        assert result.total == 1
        assert result.searched_pages == [1]
        assert result.unsupported_pages == [2]
        assert any("not evidence" in n for n in result.notes)

    def test_no_match_returns_empty_not_error(self, sample_pages):
        result = run(sample_pages, "zzzz-not-present")
        assert result.total == 0 and result.occurrences == [] and not result.truncated
