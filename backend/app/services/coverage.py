"""Find other copies of text in a document and tell whether findings cover them.

Used twice:

* **Before export** (review summary): "you approved redacting this name; here are
  occurrences that no finding covers" - the reviewer can still propose them.
* **After export** (verification): "this text still appears in the output; is that a
  location the reviewer deliberately kept (a rejected finding) or an unresolved copy?"

Results contain page numbers and counts only, never the matched text.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.models.common import BBox
from app.services.search import search_pages

OVERLAP_MIN_AREA = 0.5  # square points; ignores boxes that merely touch


def boxes_overlap(a: BBox, b: BBox) -> bool:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return w > 0 and h > 0 and w * h > OVERLAP_MIN_AREA


def any_overlap(boxes: Iterable[BBox], others: Iterable[BBox]) -> bool:
    others = list(others)
    return any(boxes_overlap(a, b) for a in boxes for b in others)


@dataclass(frozen=True)
class Copy:
    text_index: int  # index into the `texts` list passed in
    page_number: int
    boxes: tuple[BBox, ...]
    # Where the copy sits in the page text, and what the page actually says there (it can differ in case from
    # the text that was searched for). Together they let the review turn a copy into a proposal.
    char_start: int = 0
    char_end: int = 0
    text: str = ""


def find_copies(pages: Sequence, texts: Sequence[str], *, document_id: str = "-") -> list[Copy]:
    """All whole-word, case-insensitive occurrences of each text."""
    copies: list[Copy] = []
    for idx, text in enumerate(texts):
        if not text.strip():
            continue
        result = search_pages(
            pages, text, document_id=document_id, document_version=0, case_sensitive=False, whole_word=True,
            max_results=1000,
        )
        for occ in result.occurrences:
            copies.append(
                Copy(
                    text_index=idx,
                    page_number=occ.page_number,
                    boxes=tuple(occ.boxes),
                    char_start=occ.char_start,
                    char_end=occ.char_end,
                    text=occ.text,
                )
            )
    return copies
