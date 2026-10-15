"""Turn a policy written in Markdown into retrieval-sized chunks.

Why chunk at all?
-----------------
Retrieval returns *passages*, not whole files. Each chunk should be small enough
to be about one idea (so its embedding is meaningful) and small enough that the
embedding model reads all of it: ``all-MiniLM-L6-v2`` silently ignores anything
past 256 tokens. We therefore split along the policy's own headings first, and
only split inside a section when it is longer than ``MAX_WORDS_PER_CHUNK`` words
(about 200 tokens for ordinary prose).

Everything here is a plain function of the text: no database, no model. That
keeps it easy to test with odd inputs (Unicode, empty sections, huge paragraphs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Bump this whenever a change to this module could make the SAME policy text split into different chunks.
# Each stored policy version records the value it was chunked with; existing versions are never re-chunked in
# place, because their chunk ids ("policy:version:ordinal") are what findings cite and must keep pointing at the
# same words. A policy chunked by an older version is flagged instead, and the fix is to upload it as a new version.
CHUNKER_VERSION = 1

MAX_WORDS_PER_CHUNK = 140
MAX_CHUNKS_PER_POLICY = 300
INTRO_SECTION = "Introduction"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_FRONT_MATTER_KEYS = {"policy_id", "title", "version", "status"}
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int  # 1-based position in the policy; part of the stable citation id
    section: str
    text: str
    word_count: int


# ----------------------------------------------------------------------- front matter
def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split an optional ``---`` block of ``key: value`` lines from the Markdown body.

    Only a few known keys are read; everything else in the block is ignored, and
    nothing in it is ever executed or interpreted as anything but plain text.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for end in range(1, len(lines)):
        line = lines[end]
        if line.strip() == "---":
            return meta, "\n".join(lines[end + 1 :])
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if sep and key in _FRONT_MATTER_KEYS:
            meta[key] = value.strip().strip("\"'").strip()
    return {}, text  # block never closed: treat the whole file as body


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64].strip("-")


def is_valid_policy_id(value: str) -> bool:
    return bool(_SLUG_OK.match(value))


def is_valid_version(value: str) -> bool:
    return bool(_VERSION_OK.match(value))


def has_control_characters(value: str) -> bool:
    return bool(_CONTROL.search(value))


def first_heading(body: str) -> str | None:
    for line in body.splitlines():
        m = _HEADING.match(line.strip())
        if m and len(m.group(1)) == 1:
            return m.group(2).strip() or None
    return None


# ---------------------------------------------------------------------------- chunking
def _clean_line(line: str) -> str:
    stripped = line.rstrip().replace("**", "")  # emphasis markers add noise to display and embeddings
    if stripped.lstrip().startswith(">"):
        stripped = stripped.lstrip()[1:].lstrip()
    return stripped


def _words(text: str) -> list[str]:
    return text.split()


def _split_long(paragraph: str, max_words: int) -> list[str]:
    """Split one oversized paragraph at sentence ends, then (if needed) at word counts."""
    pieces: list[str] = []
    current: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        words = _words(sentence)
        if len(words) > max_words:  # a single run-on sentence: cut by words
            if current:
                pieces.append(" ".join(current))
                current = []
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i : i + max_words]))
            continue
        if len(current) + len(words) > max_words and current:
            pieces.append(" ".join(current))
            current = []
        current.extend(words)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _reflow(lines: list[str]) -> str:
    """Undo the hard line wrapping of the source file: a prose paragraph becomes one line,
    while each bullet or numbered item keeps its own line."""
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not out or _LIST_ITEM.match(line):
            out.append(text)
        else:
            out[-1] += " " + text
    return "\n".join(out)


def _section_blocks(lines: list[str]) -> list[str]:
    """Group a section's lines into paragraphs (blank lines separate; lists stay together)."""
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(_reflow(current))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(_reflow(current))
    return blocks


def chunk_markdown(body: str, *, max_words: int = MAX_WORDS_PER_CHUNK) -> list[ChunkDraft]:
    """Split Markdown into chunks that never cross a heading and stay under ``max_words``."""
    sections: list[tuple[str, list[str]]] = []
    h2 = ""
    section = INTRO_SECTION
    lines: list[str] = []

    def flush() -> None:
        if any(text.strip() for text in lines):
            sections.append((section, list(lines)))
        lines.clear()

    for raw in body.splitlines():
        m = _HEADING.match(raw.strip())
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1:  # document title: not a section of its own
                flush()
                section = INTRO_SECTION
                continue
            flush()
            if level == 2:
                h2 = title
                section = title
            else:
                section = f"{h2} > {title}" if h2 else title
            continue
        lines.append(_clean_line(raw))
    flush()

    drafts: list[ChunkDraft] = []
    for name, section_lines in sections:
        current: list[str] = []
        current_words = 0

        def emit(heading: str = name) -> None:  # bound now: this runs inside the loop that rebinds `name`
            nonlocal current, current_words
            if current:
                text = "\n\n".join(current).strip()
                if text:
                    drafts.append(ChunkDraft(len(drafts) + 1, heading, text, len(_words(text))))
            current, current_words = [], 0

        for block in _section_blocks(section_lines):
            parts = _split_long(block, max_words) if len(_words(block)) > max_words else [block]
            for part in parts:
                n = len(_words(part))
                if current and current_words + n > max_words:
                    emit()
                current.append(part)
                current_words += n
        emit()
    return drafts
