"""Text normalisation and span -> rectangle mapping shared by search and detectors.

Plain-English summary
---------------------
Every page stores its text *and* the rectangle of each word, plus the word's
character offsets inside that text (see ``pdf_processing._extract_words``).

Two problems have to be solved to find "Maria Delgado-Reyes" reliably:

1. **Text differences.** The PDF may use an en dash where the user typed a hyphen,
   curly quotes instead of straight ones, or decomposed accents (``e`` + ``◌́``)
   instead of ``é``. We match on a *normalised view* of the page text and keep a
   per-character table that maps every normalised character back to the offset
   in the original text.

2. **Offsets -> boxes.** Once we know a match covers original characters
   ``[start, end)``, we look up the words that overlap that range and merge them
   into one rectangle per visual line. A name that wraps onto a second line
   therefore produces two rectangles.

Boxes are *word-granular and conservative*: if a match covers only part of a
word (``512-44-9087`` inside ``512-44-9087.``) the box covers the whole word.
Covering a little too much is safe for redaction; covering too little would
leak text.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from app.models.common import BBox
from app.models.documents import Word

# One-to-one character replacements applied to BOTH the query and the page.
# They never change string length, which keeps offsets aligned.
_PUNCTUATION_MAP = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2212": "-",  # minus sign
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",  # non-breaking space
    }
)


class PageLike(Protocol):
    """What the helpers need from a page (satisfied by PageText and ExtractedPage)."""

    page_number: int
    text: str
    words: list[Word]


def normalize_text(value: str) -> str:
    """Normalise a query string: compatibility-compose Unicode, unify punctuation."""
    return unicodedata.normalize("NFKC", value).translate(_PUNCTUATION_MAP)


@dataclass
class NormalizedPage:
    """Normalised page text plus a map back to original offsets.

    ``starts[i]`` / ``ends[i]`` give the original ``[start, end)`` range that
    normalised character ``i`` came from. For ordinary words they are exactly
    ``(k, k+1)``. If normalisation changed a word's length (rare, e.g. a
    compatibility character expanding to two), every normalised character of that
    word maps to the whole original word, so a match can never point *inside*
    text we could not align.
    """

    text: str
    starts: list[int] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)

    def original_span(self, norm_start: int, norm_end: int) -> tuple[int, int]:
        """Map a half-open normalised range to the original text."""
        return self.starts[norm_start], self.ends[norm_end - 1]


def normalize_page(page: PageLike) -> NormalizedPage:
    original = page.text
    out_chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 0  # position in the original text already copied

    def copy_separator(upto: int) -> None:
        # Separators between words are single spaces/newlines; copy them as-is.
        nonlocal cursor
        for i in range(cursor, upto):
            out_chars.append(original[i].translate(_PUNCTUATION_MAP))
            starts.append(i)
            ends.append(i + 1)
        cursor = upto

    for word in page.words:
        copy_separator(word.char_start)
        normalised = normalize_text(word.text)
        if len(normalised) == word.char_end - word.char_start:
            for k, ch in enumerate(normalised):
                out_chars.append(ch)
                starts.append(word.char_start + k)
                ends.append(word.char_start + k + 1)
        else:
            for ch in normalised:
                out_chars.append(ch)
                starts.append(word.char_start)
                ends.append(word.char_end)
        cursor = word.char_end
    copy_separator(len(original))
    return NormalizedPage(text="".join(out_chars), starts=starts, ends=ends)


@dataclass
class SpanLocation:
    boxes: list[BBox]
    partial_start: bool  # span starts inside a word rather than at its beginning
    partial_end: bool  # span ends inside a word rather than at its end

    @property
    def mappable(self) -> bool:
        return bool(self.boxes)

    @property
    def partial_word(self) -> bool:
        return self.partial_start or self.partial_end


def locate_span(page: PageLike, start: int, end: int) -> SpanLocation:
    """Return one merged rectangle per visual line for original range ``[start, end)``.

    Returns an empty ``boxes`` list when no word overlaps the range; callers must
    treat that as "cannot be mapped reliably" and route it to manual review.
    """
    if end <= start:
        return SpanLocation([], False, False)

    touched = [w for w in page.words if w.char_start < end and w.char_end > start]
    if not touched:
        return SpanLocation([], False, False)

    merged: dict[tuple[int, int], BBox] = {}
    for w in touched:  # words are already in reading order
        key = (w.block, w.line)
        box = merged.get(key)
        if box is None:
            merged[key] = w.bbox.model_copy()
        else:
            box.x0 = min(box.x0, w.bbox.x0)
            box.y0 = min(box.y0, w.bbox.y0)
            box.x1 = max(box.x1, w.bbox.x1)
            box.y1 = max(box.y1, w.bbox.y1)

    first, last = touched[0], touched[-1]
    return SpanLocation(
        boxes=list(merged.values()),
        partial_start=start > first.char_start,
        partial_end=end < last.char_end,
    )


def snippet(text: str, start: int, end: int, radius: int = 40) -> str:
    """Short single-line context around a span (treated as sensitive data)."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return " ".join(text[lo:hi].split())
