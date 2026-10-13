"""Detector tests.

Pattern expectations are derived from `sample_data/generate_samples.py`:
3 e-mails, 3 phone numbers, 1 SSN-format value, 2 address lines.

NER assertions are limited to behaviours that hold for the right reason (people
are categorised as PERSON, company names are not). They do NOT assert that NER is
perfect: on this sample the small pretrained model misses some organisations and
produces some false positives, which is exactly why every finding needs review.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.models.common import BBox
from app.models.documents import Word
from app.models.findings import Category, ReviewLabel
from app.services import detection
from app.services.detection import (
    detect_candidates,
    load_ner,
    validate_email,
    validate_phone,
    validate_ssn,
)
from app.services.pdf_processing import extract_document
from tests.conftest import SAMPLE_CONTRACT, make_text_pdf

class _FakePage:
    """Minimal page (text + word boxes) for tests that need exact characters."""

    kind = "text"
    supported = True

    def __init__(self, text: str):
        self.page_number = 1
        self.text = text
        self.words = []
        cursor, line = 0, 0
        for line_no, line_text in enumerate(text.split("\n")):
            for token in line_text.split(" "):
                start = text.index(token, cursor)
                self.words.append(
                    Word(
                        text=token,
                        bbox=BBox(x0=start, y0=line_no * 12.0, x1=start + len(token), y1=line_no * 12.0 + 10),
                        block=0,
                        line=line_no,
                        word_index=len(self.words),
                        char_start=start,
                        char_end=start + len(token),
                    )
                )
                cursor = start + len(token)
            line = line_no
        self.geometry = None


def fake_page(text: str) -> _FakePage:
    return _FakePage(text)


NER_AVAILABLE = load_ner() is not None
needs_ner = pytest.mark.skipif(not NER_AVAILABLE, reason="spaCy en_core_web_sm is not installed")


@pytest.fixture(scope="module")
def sample_pages():
    return extract_document(SAMPLE_CONTRACT.read_bytes()).pages


def detect(pages, **kw):
    return detect_candidates(pages, document_id="doc1", content_hash="hash1", **kw)


def texts(result, category):
    return [c.text for c in result.candidates if c.category == category]


class TestPatternDetectors:
    def test_sample_contract_pattern_counts(self, sample_pages):
        res = detect(sample_pages, use_ner=False)
        assert Counter(c.category for c in res.candidates) == {
            Category.EMAIL: 3,
            Category.PHONE: 3,
            Category.SSN: 1,
            Category.ADDRESS: 2,
        }

    def test_expected_values(self, sample_pages):
        res = detect(sample_pages, use_ner=False)
        assert sorted(texts(res, Category.EMAIL)) == [
            "j.alvarez@blueharbor-example.com",
            "maria.delgado@example-mail.com",
            "priya.n@blueharbor-example.com",
        ]
        assert sorted(texts(res, Category.PHONE)) == ["(206) 555-0142", "+1 (425) 555-0133", "425-555-0199"]
        assert texts(res, Category.SSN) == ["512-44-9087"]
        assert sorted(texts(res, Category.ADDRESS)) == ["4821 Harbor View Lane, Apt 7B", "Seattle, WA 98104"]

    def test_pattern_candidates_have_boxes_and_honest_labels(self, sample_pages):
        for c in detect(sample_pages, use_ner=False).candidates:
            assert c.mappable and c.boxes
            assert c.sources and c.sources[0].startswith("pattern:")
            if c.category in {Category.EMAIL, Category.PHONE, Category.SSN}:
                assert c.label == ReviewLabel.FORMAT_VALID
                assert "not verified" in c.explanation.lower() or "cannot be verified" in c.explanation.lower()
            else:
                assert c.label == ReviewLabel.REVIEW_NEEDED

    def test_candidate_ids_are_deterministic(self, sample_pages):
        a = [c.candidate_id for c in detect(sample_pages, use_ner=False).candidates]
        b = [c.candidate_id for c in detect(sample_pages, use_ner=False).candidates]
        assert a == b and len(set(a)) == len(a)

    def test_category_filter_limits_output(self, sample_pages):
        res = detect(sample_pages, categories={Category.EMAIL})
        assert {c.category for c in res.candidates} == {Category.EMAIL}
        ner = next(d for d in res.detectors if d.kind == "ner")
        assert "skipped" in ner.detail

    def test_does_not_treat_every_number_as_sensitive(self):
        page_text = [
            "Invoice 1234567890 total $18,500.00 on 2025-03-03",
            "Reference 20250303 and ticket 987654321 and code 100 200 3000x",
        ]
        res = detect(extract_document(make_text_pdf([page_text])).pages, use_ner=False)
        assert res.candidates == []  # no account/phone/SSN guesses from bare numbers

    def test_unicode_dash_phone_and_ssn_variants_are_found(self):
        # Built by hand: the base-14 PDF fonts cannot draw an en dash, so a generated
        # PDF would not actually contain the character under test.
        text = "Call 425\u2013555\u20130100 or 206.555.0111\nSSN 513\u201345\u20139088"
        page = fake_page(text)
        res = detect([page], use_ner=False)
        # Original text (with en dashes) is returned, not the normalised copy.
        assert texts(res, Category.PHONE) == ["425\u2013555\u20130100", "206.555.0111"]
        assert texts(res, Category.SSN) == ["513\u201345\u20139088"]
        assert all(c.mappable for c in res.candidates)


class TestValidators:
    @pytest.mark.parametrize(
        "value", ["000-12-3456", "666-12-3456", "900-12-3456", "999-99-9999", "123-00-4567", "123-45-0000"]
    )
    def test_implausible_ssn_rejected(self, value):
        assert not validate_ssn(value)

    def test_plausible_ssn_accepted(self):
        assert validate_ssn("512-44-9087")

    @pytest.mark.parametrize("value", ["123-456-7890", "(206) 155-0142", "911-555-0100", "206-555-01"])
    def test_implausible_phone_rejected(self, value):
        assert not validate_phone(value)

    @pytest.mark.parametrize("value", ["(206) 555-0142", "+1 425-555-0199", "206.555.0142"])
    def test_plausible_phone_accepted(self, value):
        assert validate_phone(value)

    @pytest.mark.parametrize("value", ["a@b", "a..b@example.com", ".a@example.com", "a@-bad.com", "a@example.c"])
    def test_bad_email_rejected(self, value):
        assert not validate_email(value)

    def test_good_email_accepted(self):
        assert validate_email("first.last+tag@sub.example.co")

    def test_invalid_ssn_in_a_document_is_not_emitted(self):
        res = detect(extract_document(make_text_pdf([["Bad number 000-12-3456 here"]])).pages, use_ner=False)
        assert texts(res, Category.SSN) == []


@needs_ner
class TestNer:
    def test_all_mentions_of_a_person_are_tagged_person(self, sample_pages):
        res = detect(sample_pages)
        names = [t.replace("\n", " ") for t in texts(res, Category.PERSON)]
        assert names.count("Maria Delgado-Reyes") == 5  # includes the line-wrapped one
        assert names.count("José Álvarez") == 4
        assert "Priya Natarajan" in names

    def test_wrapped_person_has_two_boxes(self, sample_pages):
        wrapped = [c for c in detect(sample_pages).candidates if c.category == Category.PERSON and "\n" in c.text]
        assert any(c.text.replace("\n", " ") == "Maria Delgado-Reyes" and len(c.boxes) == 2 for c in wrapped)

    def test_company_names_are_not_people(self, sample_pages):
        res = detect(sample_pages)
        for t in texts(res, Category.PERSON):
            assert not any(token in t for token in ("LLC", "Inc.", "Consulting", "Analytics"))
        orgs = " | ".join(texts(res, Category.ORGANIZATION))
        assert "Northwind Analytics LLC" in orgs and "Blue Harbor Consulting Inc." in orgs

    def test_ner_provenance_names_the_model_version(self, sample_pages):
        res = detect(sample_pages)
        ner = next(d for d in res.detectors if d.kind == "ner")
        assert ner.available and "en_core_web_sm@" in ner.id
        person = next(c for c in res.candidates if c.category == Category.PERSON)
        assert person.sources[0] == ner.id
        assert person.label == ReviewLabel.REVIEW_NEEDED  # no fake probability

    def test_no_numeric_confidence_is_exposed(self, sample_pages):
        dumped = detect(sample_pages).candidates[0].model_dump()
        assert not {"confidence", "score", "probability"} & set(dumped)


def test_detection_continues_with_patterns_when_ner_is_unavailable(sample_pages, monkeypatch):
    monkeypatch.setattr(detection, "load_ner", lambda: None)
    res = detect(sample_pages)
    ner = next(d for d in res.detectors if d.kind == "ner")
    assert ner.available is False
    assert any("not installed" in n for n in res.notes)
    assert len(res.candidates) == 9  # patterns only
    assert not any(c.category in {Category.PERSON, Category.ORGANIZATION} for c in res.candidates)
