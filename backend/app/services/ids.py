"""Deterministic ids.

Findings use ids derived from *what they point at*, not random uuids. Proposing
the same span twice (for example when a request is retried) produces the same id,
so the database can refuse the duplicate instead of storing it twice.
"""

from __future__ import annotations

import hashlib
import unicodedata


def _digest(*parts: object, length: int = 16) -> str:
    raw = "\x1f".join(str(p) for p in parts).encode()
    return hashlib.sha256(raw).hexdigest()[:length]


def make_finding_id(document_id: str, content_hash: str, page: int, start: int, end: int, category: str) -> str:
    return "fnd_" + _digest(document_id, content_hash, page, start, end, category)


def make_manual_finding_id(document_id: str, content_hash: str, page: int, box: tuple[float, float, float, float]) -> str:
    rounded = tuple(round(v, 1) for v in box)
    return "fnd_" + _digest(document_id, content_hash, page, rounded, "MANUAL")


def group_key(category: str, text: str) -> str:
    """Identical mentions of the same thing share a key (case/accents/whitespace folded)."""
    folded = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return _digest(category, folded, length=12)
