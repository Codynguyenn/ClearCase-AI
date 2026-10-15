"""Check an assistant answer's citations against what its tools actually returned in this run.

The model is asked to cite in two shapes (see ``prompts.py``):

* a document page: ``[p3]``
* a policy passage, with the sentence it relies on quoted immediately before the marker:
  ``"Client names must be redacted." [northwind-redaction-policy:1.0:3]``

This module parses those markers back out and decides, with plain code, which are **verified**:

* ``[p3]`` is verified only if a tool in this run returned something from page 3.
* ``[policy:version:n]`` is verified only if ``retrieve_policy_guidance`` returned that passage in this run. A passage that
  exists in the database but was never shown to the model does not count.
* a quoted sentence is verified only if it really appears in the cited passage (ignoring runs of whitespace and the
  difference between straight and curly quotes). A "quote" that was paraphrased, merged or invented fails.

What this does NOT prove: that the model understood the passage, or that every claim in the answer is cited. It proves
that a citation is real and where it came from; the reviewer can open the source. Unverified citations are kept (the
reviewer should see that the model claimed them) but are labelled as such and counted on the run.
"""

from __future__ import annotations

import re

from app.agent.loop import RunEvidence
from app.models.conversations import Citation, CitationKind

MAX_CITATIONS = 50

_POLICY_ID = r"[a-z0-9][a-z0-9-]{0,63}:[A-Za-z0-9][A-Za-z0-9._-]{0,19}:[1-9][0-9]{0,5}"
_PAGE = re.compile(r"\[p([1-9][0-9]{0,4})\]")
_POLICY = re.compile(rf"\[({_POLICY_ID})\]")
# A quoted sentence followed (optionally after spaces) by a policy marker.
_QUOTED_POLICY = re.compile(rf"[\"“]([^\"“”\n]{{3,2000}})[\"”]\s*\[({_POLICY_ID})\]")

_QUOTE_MARKS = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def normalize_for_quote(text: str) -> str:
    """Whitespace runs collapse to one space and curly quotes become straight; nothing else is forgiven."""
    return re.sub(r"\s+", " ", text.translate(_QUOTE_MARKS)).strip()


def verify_citations(text: str, evidence: RunEvidence) -> tuple[list[Citation], int]:
    """Return ``(citations, unverified_count)`` for an answer. Citations appear in the order they first occur."""
    found: list[tuple[int, Citation]] = []

    quoted_spans: list[tuple[int, int]] = []
    for match in _QUOTED_POLICY.finditer(text):
        quote, ref = match.group(1), match.group(2)
        passage = evidence.policy_passages.get(ref)
        verified = passage is not None and normalize_for_quote(quote) in normalize_for_quote(passage)
        found.append((match.start(), Citation(kind=CitationKind.POLICY, ref=ref, verified=verified, quote=quote[:500])))
        quoted_spans.append((match.start(2) - 1, match.end(2) + 1))  # the marker already counted with its quote

    for match in _POLICY.finditer(text):
        if any(start <= match.start() < end for start, end in quoted_spans):
            continue
        ref = match.group(1)
        found.append((match.start(), Citation(kind=CitationKind.POLICY, ref=ref, verified=ref in evidence.policy_passages)))

    for match in _PAGE.finditer(text):
        page = int(match.group(1))
        found.append((match.start(), Citation(kind=CitationKind.PAGE, ref=str(page), verified=page in evidence.pages)))

    ordered: list[Citation] = []
    seen: set[tuple[str, str, str | None]] = set()
    for _, citation in sorted(found, key=lambda item: item[0]):
        key = (citation.kind.value, citation.ref, citation.quote)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(citation)
        if len(ordered) >= MAX_CITATIONS:
            break
    return ordered, sum(1 for c in ordered if not c.verified)
