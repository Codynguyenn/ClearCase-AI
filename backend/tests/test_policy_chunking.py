"""Tests for turning Markdown policies into retrieval chunks (pure functions: no database, no model)."""

from __future__ import annotations

from pathlib import Path

from app.services.policy_chunking import (
    MAX_WORDS_PER_CHUNK,
    chunk_markdown,
    first_heading,
    has_control_characters,
    is_valid_policy_id,
    is_valid_version,
    parse_front_matter,
    slugify,
)

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data" / "policies"


def test_front_matter_is_read_and_removed_from_the_body():
    text = '---\npolicy_id: acme-policy\ntitle: "Acme Policy"\nversion: "2.1"\nstatus: FICTIONAL EXAMPLE\nevil: ignored\n---\n# Heading\n\nBody.'
    meta, body = parse_front_matter(text)
    assert meta == {"policy_id": "acme-policy", "title": "Acme Policy", "version": "2.1", "status": "FICTIONAL EXAMPLE"}
    assert body.strip().startswith("# Heading")


def test_unclosed_or_missing_front_matter_means_everything_is_body():
    for text in ("no front matter here", "---\npolicy_id: x\nstill going and never closed"):
        meta, body = parse_front_matter(text)
        assert meta == {} and body == text


def test_chunks_follow_headings_and_get_stable_ordinals():
    body = "# Title\n\nIntro text.\n\n## 1. First\n\nAlpha.\n\n## 2. Second\n\nBeta.\n\n### Detail\n\nGamma."
    chunks = chunk_markdown(body)
    assert [(c.ordinal, c.section) for c in chunks] == [
        (1, "Introduction"),
        (2, "1. First"),
        (3, "2. Second"),
        (4, "2. Second > Detail"),
    ]
    assert chunk_markdown(body) == chunks  # deterministic: citation ids must not change between runs


def test_a_chunk_never_crosses_a_heading():
    chunks = chunk_markdown("## A\n\nalpha one.\n\n## B\n\nbeta two.")
    assert [c.text for c in chunks] == ["alpha one.", "beta two."]


def test_long_sections_are_split_below_the_word_limit_on_sentence_ends():
    sentence = "This sentence has exactly nine short words in it. "
    total_words = 60 * len(sentence.split())
    body = "## Long\n\n" + sentence * 60  # one very long paragraph
    chunks = chunk_markdown(body)
    assert len(chunks) > 3
    assert all(c.word_count <= MAX_WORDS_PER_CHUNK for c in chunks)
    assert all(c.text.endswith("in it.") for c in chunks)  # split between sentences, not mid-sentence
    assert sum(c.word_count for c in chunks) == total_words  # nothing lost or duplicated


def test_a_single_run_on_sentence_is_still_split():
    chunks = chunk_markdown("## Run on\n\n" + " ".join(f"w{i}" for i in range(500)))
    assert all(c.word_count <= MAX_WORDS_PER_CHUNK for c in chunks)
    assert sum(c.word_count for c in chunks) == 500


def test_small_paragraphs_in_one_section_are_packed_together():
    body = "## S\n\n" + "\n\n".join(f"Short paragraph number {i}." for i in range(5))
    chunks = chunk_markdown(body)
    assert len(chunks) == 1 and chunks[0].text.count("Short paragraph") == 5


def test_empty_and_heading_only_input_gives_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("# Only a title\n\n## Empty section\n") == []


def test_unicode_is_preserved():
    chunks = chunk_markdown("## Données personnelles\n\nLes noms de José Álvarez et 山田太郎 doivent être masqués.")
    assert chunks[0].section == "Données personnelles"
    assert "José Álvarez" in chunks[0].text and "山田太郎" in chunks[0].text


def test_markdown_emphasis_markers_and_quote_prefixes_are_removed_from_text():
    chunk = chunk_markdown("## S\n\n> **Important** notice here.")[0]
    assert chunk.text == "Important notice here."


def test_identifier_validation_rejects_unsafe_values():
    assert is_valid_policy_id("northwind-redaction-policy")
    for bad in ("", "Has Upper", "../etc", "a" * 65, "-leading", "has space", "semi;colon"):
        assert not is_valid_policy_id(bad), bad
    assert is_valid_version("1.0") and is_valid_version("2024-06_rc1")
    for bad in ("", "1 0", "../1", "x" * 21, "$ne"):
        assert not is_valid_version(bad), bad
    assert slugify("My Policy (Final)!.md") == "my-policy-final-md"
    assert first_heading("text\n# The Title\n## Sub") == "The Title"
    assert has_control_characters("a\x00b") and not has_control_characters("tab\tand newline\nok")


def test_hard_wrapped_prose_is_joined_but_list_items_keep_their_lines():
    body = (
        "## Contact\n\nPersonal details must be\nredacted when shared,\nincluding notice blocks:\n\n"
        "- email addresses,\n- telephone numbers,\n- postal addresses.\n"
    )
    text = chunk_markdown(body)[0].text
    assert "Personal details must be redacted when shared, including notice blocks:" in text
    assert "- email addresses,\n- telephone numbers,\n- postal addresses." in text


def test_the_shipped_sample_policies_chunk_cleanly():
    for path in SAMPLES.glob("*.md"):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        chunks = chunk_markdown(body)
        assert meta["policy_id"] and meta["version"] and "FICTIONAL" in meta["status"].upper()
        assert len(chunks) >= 8
        assert all(c.word_count <= MAX_WORDS_PER_CHUNK and c.text.strip() for c in chunks)
        # The file's own hard line wrapping must be gone: a line may only follow another line
        # directly if it is a bullet/numbered item.
        for c in chunks:
            lines = c.text.split("\n")
            for i, line in enumerate(lines[1:], start=1):
                if line.strip() and lines[i - 1].strip():
                    assert line.lstrip().startswith(("-", "*", "+")) or line.lstrip()[:1].isdigit(), (c.citation if hasattr(c, "citation") else c.ordinal, line[:40])
        assert [c.ordinal for c in chunks] == list(range(1, len(chunks) + 1))
