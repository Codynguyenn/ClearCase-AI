"""One-line, safe descriptions of tool calls for the activity log.

The activity log is stored and shown to the reviewer, so a summary may only contain counts, page numbers, and short
fixed vocabulary (category names, status words). It must never contain document text, search queries, excerpts,
policy wording or error text from a tool. Every function below reads only numbers and enum-like fields.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.agent.mcp_client import ToolCallResult

MAX_PAGES_LISTED = 12


def _pages(values: list[int]) -> str:
    unique = sorted({v for v in values if isinstance(v, int)})
    shown = ", ".join(str(v) for v in unique[:MAX_PAGES_LISTED])
    return shown + (", ..." if len(unique) > MAX_PAGES_LISTED else "")


def _count(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    return len(value) if isinstance(value, list) else 0


def _search(d: dict[str, Any]) -> str:
    pages = [o.get("page_number") for o in d.get("occurrences", []) if isinstance(o, dict)]
    more = ", more exist" if d.get("truncated") else ""
    where = f" on page(s) {_pages(pages)}" if pages else ""
    skipped = _count(d, "unsupported_pages")
    return f"{d.get('returned', 0)} match(es){where}{more}; {skipped} page(s) not searched"


def _excerpt(d: dict[str, Any]) -> str:
    length = int(d.get("end", 0)) - int(d.get("start", 0))
    more = ", more available" if d.get("has_more") else ""
    return f"page {d.get('page_number')}, {max(length, 0)} characters{more}"


def _detect(d: dict[str, Any]) -> str:
    by_category = Counter(c.get("category") for c in d.get("candidates", []) if isinstance(c, dict))
    parts = ", ".join(f"{name} {n}" for name, n in sorted(by_category.items()) if isinstance(name, str))
    return f"{d.get('returned', 0)} of {d.get('total_candidates', 0)} candidate(s)" + (f": {parts}" if parts else "")


def _policy(d: dict[str, Any]) -> str:
    return f"status {d.get('status')}; {_count(d, 'passages')} passage(s)"


def _propose(d: dict[str, Any]) -> str:
    return f"{_count(d, 'created')} created, {_count(d, 'already_existed')} already existed, {_count(d, 'refused')} refused"


def _review(d: dict[str, Any]) -> str:
    summary = d.get("summary") if isinstance(d.get("summary"), dict) else {}
    return f"{summary.get('unresolved', 0)} undecided, {summary.get('approved', 0)} approved"


def _export(d: dict[str, Any]) -> str:
    return f"status {d.get('status')}; {d.get('approved_count', 0)} approved; downloadable {bool(d.get('downloadable'))}"


_SUMMARIZERS = {
    "search_document": _search,
    "get_document_excerpt": _excerpt,
    "detect_sensitive_entities": _detect,
    "retrieve_policy_guidance": _policy,
    "propose_redactions": _propose,
    "get_review_summary": _review,
    "export_approved_redactions": _export,
}


def summarize_tool_call(name: str, result: ToolCallResult) -> str:
    if not result.ok:
        return f"failed: {result.error_code or 'error'}"
    try:
        summarizer = _SUMMARIZERS.get(name)
        if summarizer and result.data is not None:
            return summarizer(result.data)
    except Exception:  # noqa: BLE001 - a summary must never be able to break a run
        pass
    return "completed"
