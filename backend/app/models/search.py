"""Search request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.common import ApiModel, BBox


class Occurrence(ApiModel):
    """One literal match of a query in a document.

    ``occurrence_id`` is deterministic for (document, version, page, span), so the
    same search returns the same ids and a decision can refer to one occurrence.
    ``text`` and ``context`` come from the user's own document and are treated as
    sensitive application data.
    """

    occurrence_id: str
    page_number: int
    char_start: int
    char_end: int
    text: str
    context: str
    boxes: list[BBox]
    mappable: bool = Field(description="false if no rectangle could be derived; needs manual review")
    partial_word: bool = Field(
        description="match begins/ends inside a word; boxes cover the whole word (conservative)"
    )


class SearchResult(ApiModel):
    query: str
    case_sensitive: bool
    whole_word: bool
    total: int
    truncated: bool
    occurrences: list[Occurrence]
    searched_pages: list[int]
    unsupported_pages: list[int] = Field(
        description="pages that are scanned/mixed/empty; text on them may be missing or partial"
    )
    notes: list[str] = Field(default_factory=list)


class SearchParams(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    case_sensitive: bool = False
    whole_word: bool = False
