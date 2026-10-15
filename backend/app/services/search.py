"""Literal document search.

Design choices (plain English):

* **Literal, not regex.** The user's text is escaped and matched exactly. Whitespace
  in the query matches any run of spaces/newlines so "Maria Delgado-Reyes" is found
  even when the name wraps across two lines. We never compile a user-supplied
  regular expression, so there is no catastrophic-backtracking risk.
* **Bounded.** Query length and result count are capped; hitting the cap is
  reported (``truncated``) rather than hidden.
* **Honest about coverage.** The result lists which pages were searched and which
  pages are unsupported (scanned/mixed/empty), so "0 results" is never mistaken
  for "not in the document".
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Protocol

from app.models.search import Occurrence, SearchResult
from app.services.errors import ValidationFailed
from app.services.spans import PageLike, locate_span, normalize_page, normalize_text, snippet

MAX_QUERY_CHARS = 200
DEFAULT_MAX_RESULTS = 500


class SearchablePage(PageLike, Protocol):
    kind: str
    supported: bool


def make_occurrence_id(document_id: str, version: int, page_number: int, start: int, end: int) -> str:
    """Stable id: same document version + page + span always yields the same id."""
    raw = f"{document_id}:{version}:{page_number}:{start}:{end}".encode()
    return "occ_" + hashlib.sha256(raw).hexdigest()[:16]


def build_pattern(query: str, *, case_sensitive: bool, whole_word: bool) -> re.Pattern[str]:
    if len(query) > MAX_QUERY_CHARS:
        raise ValidationFailed(f"Query is longer than {MAX_QUERY_CHARS} characters.", code="query_too_long")
    tokens = normalize_text(query).split()
    if not tokens:
        raise ValidationFailed("Query must contain at least one non-space character.", code="empty_query")

    body = r"\s+".join(re.escape(t) for t in tokens)
    # Word-boundary guards only make sense on sides that begin/end with a word
    # character; otherwise a query like "(206)" could never match.
    prefix = r"(?<!\w)" if whole_word and _is_word_char(tokens[0][0]) else ""
    suffix = r"(?!\w)" if whole_word and _is_word_char(tokens[-1][-1]) else ""
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(prefix + body + suffix, flags)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def search_pages(
    pages: Sequence[SearchablePage],
    query: str,
    *,
    document_id: str,
    document_version: int,
    case_sensitive: bool = False,
    whole_word: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> SearchResult:
    pattern = build_pattern(query, case_sensitive=case_sensitive, whole_word=whole_word)

    occurrences: list[Occurrence] = []
    searched: list[int] = []
    unsupported: list[int] = []
    truncated = False

    for page in pages:
        if not page.supported:
            unsupported.append(page.page_number)
        if not page.words:
            continue  # nothing to search (scanned/empty page)
        searched.append(page.page_number)

        norm = normalize_page(page)
        for match in pattern.finditer(norm.text):
            if len(occurrences) >= max_results:
                truncated = True
                break
            start, end = norm.original_span(match.start(), match.end())
            loc = locate_span(page, start, end)
            occurrences.append(
                Occurrence(
                    occurrence_id=make_occurrence_id(document_id, document_version, page.page_number, start, end),
                    page_number=page.page_number,
                    char_start=start,
                    char_end=end,
                    text=page.text[start:end],
                    context=snippet(page.text, start, end),
                    boxes=loc.boxes,
                    mappable=loc.mappable,
                    partial_word=loc.partial_word,
                )
            )
        if truncated:
            break

    notes: list[str] = []
    if unsupported:
        notes.append(
            f"Pages {', '.join(map(str, unsupported))} are scanned, mixed or empty. "
            "Text in images was not searched, so absence of a match on those pages is not evidence."
        )
    if truncated:
        notes.append(f"Results were capped at {max_results}; refine the query to see the rest.")
    notes.append("Hyphenated words split across lines are not rejoined.")

    return SearchResult(
        query=query,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        total=len(occurrences),
        truncated=truncated,
        occurrences=occurrences,
        searched_pages=searched,
        unsupported_pages=unsupported,
        notes=notes,
    )
