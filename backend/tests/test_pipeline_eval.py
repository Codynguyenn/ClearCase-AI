"""The pipeline measurement: its scoring helpers are tested like product code (a wrong rule would silently mislead the report)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[2] / "evaluation"
sys.path.insert(0, str(EVAL))

from assistant_eval.metrics import Item, Proposal, score_document  # noqa: E402
from pipeline_eval import run as pe  # noqa: E402

PERSON = {"page": 1, "start": 10, "end": 30, "kind": "PERSON", "keep": False}
ORG = {"page": 1, "start": 40, "end": 60, "kind": "ORG", "keep": True}
TRUTH = [PERSON, ORG]


# ----------------------------------------------------------------------------- attribution and the simulated reviewer
def test_a_proposal_inside_an_item_is_attributed_to_it():
    assert pe.attribute(1, 10, 30, TRUTH) is PERSON
    assert pe.attribute(1, 12, 28, TRUTH) is PERSON


def test_a_proposal_must_lie_at_least_half_inside_the_item():
    assert pe.attribute(1, 20, 40, TRUTH) is PERSON  # 10 of 20 characters inside: exactly half counts
    assert pe.attribute(1, 21, 41, TRUTH) is None  # 9 of 20 inside


def test_other_pages_and_empty_overlaps_are_not_attributed():
    assert pe.attribute(2, 10, 30, TRUTH) is None
    assert pe.attribute(1, 0, 5, TRUTH) is None


def test_the_item_with_the_largest_overlap_wins():
    near = [{"page": 1, "start": 0, "end": 12, "kind": "PERSON", "keep": False}, {"page": 1, "start": 8, "end": 30, "kind": "EMAIL", "keep": False}]
    assert pe.attribute(1, 5, 25, near)["kind"] == "EMAIL"


def test_the_reviewer_approves_personal_information_and_rejects_company_names_and_strays():
    assert pe.reviewer_decision(1, 10, 30, TRUTH) == "approve"
    assert pe.reviewer_decision(1, 40, 60, TRUTH) == "reject"  # a company name must stay visible
    assert pe.reviewer_decision(1, 100, 110, TRUTH) == "reject"  # points at nothing


def test_the_reviewer_rule_agrees_with_the_assistant_evaluation_scoring():
    """Same attribution rule as `metrics.score_document`: approved proposals == its 'correct' count."""
    items = [Item(d["page"], d["start"], d["end"], d["kind"], d["keep"]) for d in TRUTH]
    spans = [(1, 10, 30), (1, 15, 25), (1, 40, 60), (1, 100, 110), (1, 25, 45), (2, 10, 30), (1, 29, 50)]
    score = score_document(items, [Proposal(*s) for s in spans])
    approved = sum(pe.reviewer_decision(*s, TRUTH) == "approve" for s in spans)
    assert approved == score.correct


# ----------------------------------------------------------------------------- the independent re-check of the output
def test_approved_text_still_readable_counts_every_leftover_occurrence():
    assert pe.approved_still_readable("Ana Cole met Ana Cole.", ["Ana Cole"], []) == 2
    assert pe.approved_still_readable("nothing here", ["Ana Cole"], []) == 0


def test_approved_text_inside_a_kept_company_name_is_not_a_leftover():
    assert pe.approved_still_readable("Cole Partners LLP and Cole", ["Cole"], ["Cole Partners LLP"]) == 1
    assert pe.approved_still_readable("Cole Partners LLP", ["Cole"], ["Cole Partners LLP"]) == 0


def test_the_longest_kept_name_is_removed_first_so_nested_names_do_not_hide_leftovers():
    assert pe.approved_still_readable("Cole Partners LLP", ["Partners"], ["Cole", "Cole Partners LLP"]) == 0


def test_empty_approved_text_is_ignored():
    assert pe.approved_still_readable("abc", ["", "b"], []) == 1


def test_kept_names_preserved_is_capped_at_the_original_count():
    assert pe.keep_preserved("Acme Acme Acme", "Acme", ["Acme"]) == (1, 3)  # two of three occurrences were lost
    assert pe.keep_preserved("Acme", "Acme Acme", ["Acme"]) == (1, 1)  # extra text cannot raise the rate above 1
    assert pe.keep_preserved("Acme", "Acme", ["Acme", "Acme"]) == (1, 1)  # a name listed twice counts once


def test_texts_with_unapproved_occurrences_finds_partially_proposed_texts():
    original = "Ana Cole signed. Ana Cole agreed. Bo Ray left."
    assert pe.texts_with_unapproved_occurrences(original, ["Ana Cole", "Ana Cole", "Bo Ray"]) == 0
    assert pe.texts_with_unapproved_occurrences(original, ["Ana Cole", "Bo Ray"]) == 1  # one Ana Cole was never proposed


# ----------------------------------------------------------------------------- statistics and the aggregate
def test_percentile_and_stats():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert pe.percentile(values, 0.9) == 90 and pe.percentile(values, 1.0) == 100 and pe.percentile([5], 0.9) == 5
    s = pe.stats(values)
    assert (s["median"], s["max"], s["min"], s["n"]) == (55, 100, 10, 10)


def row(doc_id, outcome="verified", **extra):
    base = {
        "split": "dev", "doc_id": doc_id, "template": "t", "pages": 1, "ms_extract_library": 5.0, "ms_upload_to_ready": 100.0, "ms_detect_pattern_only": 1.0,
        "ms_detect_with_ner": 10.0, "ms_propose_detected": 20.0, "ms_export": 200.0, "proposed": 4, "approved": 3, "rejected": 1, "outcome": outcome,
        "needed_acknowledgement": outcome == "verified_needs_acknowledgement", "unresolved_copies": 0, "required_checks_failed": [], "advisory_checks_failed": [],
        "checks": 8, "texts_with_unapproved_occurrences": 0, "approved_still_readable": 0, "keep_preserved": 2, "keep_total": 2, "sensitive_items": 5, "sensitive_covered": 3,
        "items_by_category": {"PERSON": 3, "PHONE": 2}, "covered_by_category": {"PERSON": 3},
    }  # fmt: skip
    return base | extra


def test_the_aggregate_counts_outcomes_and_sums_the_rechecks():
    rows = [row("a"), row("b", "verified_needs_acknowledgement", texts_with_unapproved_occurrences=1), row("c", "verification_failed", approved_still_readable=None, keep_preserved=None, keep_total=None)]
    agg = pe.aggregate([rows, rows])
    assert agg["outcomes"] == {"verified": 1, "verified_needs_acknowledgement": 1, "verification_failed": 1}
    assert agg["needed_acknowledgement"] == ["b"] and agg["ack_cause_is_partial_proposal"] is True
    assert agg["approved_text_still_readable"] == {"occurrences": 0, "documents_checked": 2}  # the failed export produced no file to check
    assert agg["keep_preserved"] == {"kept": 4, "of": 4} and agg["sensitive_items"] == 15 and agg["sensitive_covered"] == 9
    assert agg["coverage_by_category"]["PERSON"] == {"items": 9, "covered": 9} and agg["coverage_by_category"]["PHONE"] == {"items": 6, "covered": 0}
    assert agg["coverage_by_category"]["EMAIL"] == {"items": 0, "covered": 0}


def test_the_aggregate_uses_the_median_over_passes_per_document():
    agg = pe.aggregate([[row("a", ms_export=100.0)], [row("a", ms_export=100.0)], [row("a", ms_export=400.0)]])  # one slow outlier pass
    assert agg["latency_ms"]["ms_export"]["median"] == 100.0 and agg["passes"] == 3  # a mean would say 200


def test_outcomes_that_change_between_passes_are_flagged():
    agg = pe.aggregate([[row("a")], [row("a", "verification_failed")]])
    assert agg["outcomes_stable_across_passes"] is False


def test_each_prediction_is_evaluated_against_its_own_threshold():
    def agg(rows):
        return pe.aggregate([rows])

    good = [row(str(i)) for i in range(18)]
    assert [p["held"] for p in pe.predictions(agg(good))] == [True, True, True, True]
    assert pe.predictions(agg(good[:14] + [row("x", "verified_needs_acknowledgement", texts_with_unapproved_occurrences=1)] * 4))[0]["held"] is False  # only 14 clean
    assert pe.predictions(agg(good[:17] + [row("x", "verification_failed")]))[0]["held"] is False
    assert pe.predictions(agg(good[:17] + [row("x", approved_still_readable=1)]))[1]["held"] is False
    assert pe.predictions(agg(good[:17] + [row("x", keep_preserved=1, keep_total=2)]))[1]["held"] is False
    assert pe.predictions(agg([row(str(i), ms_upload_to_ready=1500.0) for i in range(18)]))[2]["held"] is False
    assert pe.predictions(agg([row(str(i), ms_export=2500.0) for i in range(18)]))[2]["held"] is False
    unexplained = good[:17] + [row("x", "verified_needs_acknowledgement", texts_with_unapproved_occurrences=0)]
    assert pe.predictions(agg(unexplained))[3]["held"] is False


# ----------------------------------------------------------------------------- the documents
def test_all_eighteen_synthetic_documents_load_with_their_pdfs():
    docs = pe.load_documents()
    assert len(docs) == 18
    for _split, doc, pdf in docs:
        assert pdf.startswith(b"%PDF") and doc["ground_truth"]


def test_the_scratch_database_is_not_the_test_or_the_real_one():
    assert pe.SCRATCH_DB not in ("clearcase", "clearcase_test")
    assert pytest.approx(pe.ATTRIBUTION_MIN) == 0.5
