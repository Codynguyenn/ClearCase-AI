"""Citation checking: a citation is verified only against what the tools returned in the same run."""

from __future__ import annotations

import pytest

from app.agent.citations import normalize_for_quote, verify_citations
from app.agent.loop import RunEvidence
from app.models.conversations import Citation, CitationKind

PASSAGE_ID = "northwind-redaction-policy:1.0:3"
OTHER_ID = "northwind-sharing-guidelines:1.0:2"
PASSAGE = "Names of individuals must be redacted\nwhen a document is shared outside the company.\nJob titles may stay visible."
OTHER = "A second person must check the document before it is sent."


def evidence(pages=(1, 2, 3), passages=None) -> RunEvidence:
    return RunEvidence(pages=set(pages), policy_passages=passages if passages is not None else {PASSAGE_ID: PASSAGE, OTHER_ID: OTHER})


def check(text: str, ev: RunEvidence | None = None):
    return verify_citations(text, ev or evidence())


def one(text: str, ev: RunEvidence | None = None) -> Citation:
    citations, _ = check(text, ev)
    assert len(citations) == 1, citations
    return citations[0]


# ----------------------------------------------------------------------------- pages
def test_a_page_the_tools_returned_is_verified_and_one_they_did_not_is_not():
    citations, unverified = check("Seen on [p2], and also claimed on [p9].")
    assert [(c.kind, c.ref, c.verified) for c in citations] == [(CitationKind.PAGE, "2", True), (CitationKind.PAGE, "9", False)]
    assert unverified == 1


# ----------------------------------------------------------------------------- policy passages
def test_a_policy_passage_is_verified_only_if_it_was_retrieved_in_this_run():
    assert one(f"See [{PASSAGE_ID}].").verified
    never_shown = "northwind-redaction-policy:1.0:7"  # may exist in the database, but the model was never shown it
    assert not one(f"See [{never_shown}].").verified


def test_an_exact_quote_of_the_cited_passage_is_verified():
    c = one(f'The policy says "Job titles may stay visible." [{PASSAGE_ID}]')
    assert c.verified and c.quote == "Job titles may stay visible."


def test_a_fragment_of_a_real_sentence_is_still_a_real_quote():
    assert one(f'It requires that "must be redacted" [{PASSAGE_ID}] for sharing.').verified


def test_line_breaks_and_curly_quotes_are_forgiven_nothing_else_is():
    assert one(f"It says “Names of individuals must be redacted when a document is shared outside the company.” [{PASSAGE_ID}]").verified
    passage = {PASSAGE_ID: "The firm’s names must be redacted."}
    assert one(f"\"The firm's names must be redacted.\" [{PASSAGE_ID}]", evidence(passages=passage)).verified


@pytest.mark.parametrize(
    "quote",
    [
        "Names of individuals should be redacted",  # a changed word
        "Names of individuals must be redacted and job titles removed",  # real start, invented end
        "Job titles must stay visible",  # altered
        "names of individuals must be redacted",  # case is not forgiven
        "Company names must always be redacted in every document shared with anyone",  # invented
    ],
)
def test_a_paraphrased_or_altered_or_invented_quote_is_not_verified(quote: str):
    c = one(f'The policy says "{quote}" [{PASSAGE_ID}]')
    assert not c.verified and c.quote == quote


def test_a_real_quote_attributed_to_the_wrong_passage_is_not_verified():
    assert not one(f'"A second person must check the document" [{PASSAGE_ID}]').verified  # it is in OTHER, not in PASSAGE
    assert one(f'"A second person must check the document" [{OTHER_ID}]').verified


def test_a_very_long_invented_quote_cannot_slip_past_the_check():
    invented = ("this sentence was never in any policy " * 20).strip()  # ~760 characters
    c = one(f'"{invented}" [{PASSAGE_ID}]')
    assert not c.verified and c.quote is not None and len(c.quote) <= 500


def test_a_quote_whose_passage_was_not_retrieved_is_unverified_even_if_the_words_exist_elsewhere():
    ev = evidence(passages={OTHER_ID: OTHER})
    assert not one(f'"Job titles may stay visible." [{PASSAGE_ID}]', ev).verified


# ----------------------------------------------------------------------------- structure
def test_a_quoted_marker_is_one_citation_not_two():
    citations, _ = check(f'"Job titles may stay visible." [{PASSAGE_ID}]')
    assert len(citations) == 1 and citations[0].quote


def test_the_same_citation_repeated_is_listed_once_but_quoted_and_bare_uses_are_separate():
    citations, _ = check(f'See [p1] and [p1] again. Rule: "Job titles may stay visible." [{PASSAGE_ID}] and later [{PASSAGE_ID}].')
    assert [(c.kind.value, c.ref, c.quote is not None) for c in citations] == [
        ("page", "1", False),
        ("policy", PASSAGE_ID, True),
        ("policy", PASSAGE_ID, False),
    ]


def test_citations_are_returned_in_the_order_they_first_appear():
    citations, _ = check(f"First [p3], then [{OTHER_ID}], then [p1].")
    assert [c.ref for c in citations] == ["3", OTHER_ID, "1"]


def test_text_with_no_citations_has_none_and_nothing_unverified():
    assert check("I found two email addresses and proposed both.") == ([], 0)


@pytest.mark.parametrize(
    "junk", ["[p0]", "[p]", "[P3]", "[p3 ]", "[page 3]", "[p-1]", "[northwind:1.0]", "[Northwind-Policy:1.0:3]", "[a:1.0:0]", "(p3)"]
)
def test_markers_that_are_not_in_the_documented_shape_are_ignored(junk: str):
    assert check(f"Something {junk} here.") == ([], 0)


def test_the_number_of_citations_kept_is_capped():
    text = " ".join(f"[p{n}]" for n in range(1, 200))
    citations, _ = check(text, evidence(pages=range(1, 200)))
    assert len(citations) == 50


def test_normalisation_only_collapses_whitespace_and_straightens_quotes():
    assert normalize_for_quote("  a\n\n b\t“c”  ’d’ ") == "a b \"c\" 'd'"
