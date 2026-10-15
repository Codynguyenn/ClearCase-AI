"""Candidate detection: validated patterns + a pretrained NER model.

What this module does and does not do
-------------------------------------
* It produces **candidates** with provenance. It never creates proposals and never
  decides what should be redacted; the user's request and the policy do that.
* **Pattern detectors** (email, phone, SSN-format, street address, postal line) are
  deterministic. Where a validity rule exists (e.g. SSN area numbers), a candidate
  that fails it is dropped, and one that passes is labelled ``format_valid`` -
  meaning "plausible format", *not* "verified real identifier".
* **NER** uses the pretrained spaCy ``en_core_web_sm`` model (we did not train it).
  It has no calibrated confidence, so its candidates carry the label
  ``review_needed`` and a source string naming the exact model version. We never
  invent a probability.
* If the NER model is not installed, detection continues with patterns only and
  says so in ``detectors`` / ``notes``; it never pretends NER ran.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.models.findings import (
    Candidate,
    Category,
    DetectionResult,
    DetectorInfo,
    ReviewLabel,
)
from app.services.ids import make_finding_id
from app.services.spans import PageLike, locate_span, normalize_page, snippet

logger = logging.getLogger(__name__)

NER_MODEL_NAME = "en_core_web_sm"

# spaCy label -> our category. Everything else (DATE, MONEY, CARDINAL, ...) is ignored.
_NER_LABELS: dict[str, Category] = {
    "PERSON": Category.PERSON,
    "ORG": Category.ORGANIZATION,
    "GPE": Category.LOCATION,
    "LOC": Category.LOCATION,
}


# ------------------------------------------------------------------ validators


def validate_email(value: str) -> bool:
    local, _, domain = value.rpartition("@")
    if not local or not domain or len(value) > 254 or len(local) > 64:
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not labels[-1].isalpha() or len(labels[-1]) < 2:
        return False
    return all(label and len(label) <= 63 and not label.startswith("-") and not label.endswith("-") for label in labels)


def validate_phone(value: str) -> bool:
    """North American Numbering Plan rules: area and exchange start with 2-9, no N11 area."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    area, exchange = digits[:3], digits[3:6]
    if area[0] in "01" or exchange[0] in "01":
        return False
    return area[1:] != "11"


def validate_ssn(value: str) -> bool:
    """Plausible-SSN rules from the SSA: area not 000/666/9xx, group not 00, serial not 0000.

    Passing means "could be an SSN", not "is a real person's SSN".
    """
    area, group, serial = value.split("-")
    if area in {"000", "666"} or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


# -------------------------------------------------------------------- patterns


@dataclass(frozen=True)
class PatternDetector:
    id: str
    category: Category
    regex: re.Pattern[str]
    validate: Callable[[str], bool] | None
    label: ReviewLabel
    explanation: str


_STREET_SUFFIX = (
    "Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Place|Pl|Way|Terrace|Ter|Parkway|Pkwy"
)

PATTERN_DETECTORS: tuple[PatternDetector, ...] = (
    PatternDetector(
        id="pattern:email",
        category=Category.EMAIL,
        regex=re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9-])"
        ),
        validate=validate_email,
        label=ReviewLabel.FORMAT_VALID,
        explanation="Matches e-mail address format and passes syntax checks; not verified as a working address.",
    ),
    PatternDetector(
        id="pattern:phone_us",
        category=Category.PHONE,
        # Requires separators or parentheses so a bare 10-digit number is NOT treated as a phone.
        regex=re.compile(
            r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)[ \t]?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
        ),
        validate=validate_phone,
        label=ReviewLabel.FORMAT_VALID,
        explanation="Matches US phone number format and numbering-plan rules; not verified as a working number.",
    ),
    PatternDetector(
        id="pattern:ssn_format",
        category=Category.SSN,
        regex=re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"),
        validate=validate_ssn,
        label=ReviewLabel.FORMAT_VALID,
        explanation="Matches a plausible US Social Security number format; cannot be verified as a real identifier.",
    ),
    PatternDetector(
        id="pattern:street_address_us",
        category=Category.ADDRESS,
        regex=re.compile(
            rf"\b\d{{1,6}}[ \t]+(?:[A-Z][A-Za-z0-9.'-]*[ \t]+){{1,4}}(?:{_STREET_SUFFIX})\b\.?"
            r"(?:,?[ \t]*(?:Apt|Apartment|Suite|Ste|Unit|#)\.?[ \t]*[A-Za-z0-9-]+)?"
        ),
        validate=None,
        label=ReviewLabel.REVIEW_NEEDED,
        explanation="Looks like a US street address (number, street name, street type); needs review.",
    ),
    PatternDetector(
        id="pattern:postal_line_us",
        category=Category.ADDRESS,
        regex=re.compile(
            r"\b[A-Z][A-Za-z.'-]+(?:[ \t]+[A-Z][A-Za-z.'-]+)*,[ \t]*[A-Z]{2}[ \t]+\d{5}(?:-\d{4})?\b"
        ),
        validate=None,
        label=ReviewLabel.REVIEW_NEEDED,
        explanation="Looks like a US 'City, ST ZIP' line; needs review.",
    ),
)


# ------------------------------------------------------------------------ NER


@dataclass(frozen=True)
class NerModel:
    nlp: Any
    name: str
    version: str

    @property
    def source_id(self) -> str:
        return f"ner:spacy:{self.name}@{self.version}"


@lru_cache(maxsize=1)
def load_ner() -> NerModel | None:
    """Load the pretrained model once; return None when it is not installed."""
    try:
        import spacy

        nlp = spacy.load(NER_MODEL_NAME, disable=["lemmatizer"])
    except Exception as exc:  # noqa: BLE001 - missing package or model are both "unavailable"
        logger.warning("spaCy NER unavailable (%s). Pattern detection still works.", exc)
        return None
    return NerModel(nlp=nlp, name=NER_MODEL_NAME, version=nlp.meta.get("version", "unknown"))


def ner_info() -> DetectorInfo:
    model = load_ner()
    if model is None:
        return DetectorInfo(
            id=f"ner:spacy:{NER_MODEL_NAME}",
            kind="ner",
            available=False,
            detail=(
                f"spaCy model '{NER_MODEL_NAME}' is not installed; only pattern detection ran. "
                "See README 'One-time model downloads'."
            ),
        )
    return DetectorInfo(
        id=model.source_id,
        kind="ner",
        available=True,
        detail="Pretrained spaCy English pipeline (not trained by this project). No calibrated confidence.",
    )


def _trim_entity(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Drop surrounding whitespace and a trailing possessive; reject trivial spans."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if text[start:end].endswith(("'s", "\u2019s")):
        end -= 2
    value = text[start:end]
    if len(value.strip()) < 2 or value.isdigit():
        return None
    return start, end


# --------------------------------------------------------------------- driver


def _make_candidate(
    page: PageLike,
    *,
    document_id: str,
    content_hash: str,
    category: Category,
    start: int,
    end: int,
    source: str,
    label: ReviewLabel,
    explanation: str,
) -> Candidate:
    loc = locate_span(page, start, end)
    return Candidate(
        candidate_id=make_finding_id(document_id, content_hash, page.page_number, start, end, category.value),
        category=category,
        page_number=page.page_number,
        char_start=start,
        char_end=end,
        text=page.text[start:end],
        context=snippet(page.text, start, end),
        boxes=loc.boxes,
        mappable=loc.mappable,
        partial_word=loc.partial_word,
        sources=[source],
        label=label,
        explanation=explanation,
    )


def detect_candidates(
    pages: Sequence[Any],
    *,
    document_id: str,
    content_hash: str,
    categories: set[Category] | None = None,
    use_ner: bool = True,
) -> DetectionResult:
    wanted = categories if categories else None
    merged: dict[tuple[int, int, int, str], Candidate] = {}
    detectors: list[DetectorInfo] = []
    notes: list[str] = []

    def add(candidate: Candidate) -> None:
        key = (candidate.page_number, candidate.char_start, candidate.char_end, candidate.category.value)
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
        else:  # same span and category from two detectors: keep one, record both sources
            for s in candidate.sources:
                if s not in existing.sources:
                    existing.sources.append(s)

    text_pages = [p for p in pages if p.words]

    # ---- patterns
    for det in PATTERN_DETECTORS:
        active = wanted is None or det.category in wanted
        detectors.append(
            DetectorInfo(id=det.id, kind="pattern", available=True, detail="ran" if active else "skipped (category not requested)")
        )
        if not active:
            continue
        for page in text_pages:
            norm = normalize_page(page)
            for m in det.regex.finditer(norm.text):
                if det.validate is not None and not det.validate(m.group(0)):
                    continue
                start, end = norm.original_span(m.start(), m.end())
                add(
                    _make_candidate(
                        page,
                        document_id=document_id,
                        content_hash=content_hash,
                        category=det.category,
                        start=start,
                        end=end,
                        source=det.id,
                        label=det.label,
                        explanation=det.explanation,
                    )
                )

    # ---- NER
    ner_categories = {Category.PERSON, Category.ORGANIZATION, Category.LOCATION}
    ner_wanted = use_ner and (wanted is None or bool(wanted & ner_categories))
    info = ner_info()
    detectors.append(info if ner_wanted else info.model_copy(update={"detail": "skipped (not requested)"}))
    if ner_wanted:
        model = load_ner()
        if model is None:
            notes.append(info.detail)
        else:
            for page in text_pages:
                doc = model.nlp(page.text)
                for ent in doc.ents:
                    category = _NER_LABELS.get(ent.label_)
                    if category is None or (wanted is not None and category not in wanted):
                        continue
                    trimmed = _trim_entity(page.text, ent.start_char, ent.end_char)
                    if trimmed is None:
                        continue
                    add(
                        _make_candidate(
                            page,
                            document_id=document_id,
                            content_hash=content_hash,
                            category=category,
                            start=trimmed[0],
                            end=trimmed[1],
                            source=model.source_id,
                            label=ReviewLabel.REVIEW_NEEDED,
                            explanation=f"Named-entity model tagged this as {category.value.lower()}; confidence is not calibrated, review needed.",
                        )
                    )

    unsupported = [p.page_number for p in pages if not p.supported]
    if unsupported:
        notes.append(
            f"Pages {', '.join(map(str, unsupported))} are scanned, mixed or empty; text in images was not analysed."
        )
    notes.append("Candidates are suggestions only. Nothing is proposed or redacted until you choose it.")

    ordered = sorted(merged.values(), key=lambda c: (c.page_number, c.char_start, c.char_end, c.category.value))
    return DetectionResult(
        candidates=ordered,
        detectors=detectors,
        searched_pages=[p.page_number for p in text_pages],
        unsupported_pages=unsupported,
        notes=notes,
    )
