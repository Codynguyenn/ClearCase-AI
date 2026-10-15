"""The evaluation harness: scoring rules, dataset ground truth and the budget guard are tested like product code.

A scoring rule that is wrong would silently mislead every comparison, so each rule has a small hand-made case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[2] / "evaluation"
sys.path.insert(0, str(EVAL))

from assistant_eval import dataset, metrics  # noqa: E402
from assistant_eval.metrics import Item, Proposal, score_document, summarise  # noqa: E402

PERSON = Item(1, 10, 30, "PERSON")  # 20 characters
EMAIL = Item(1, 40, 70, "EMAIL")  # 30 characters
ORG = Item(1, 80, 100, "ORG", keep=True)  # 20 characters
TRUTH = [PERSON, EMAIL, ORG]


def sc(*proposals: Proposal):
    return score_document(TRUTH, list(proposals))


# ----------------------------------------------------------------------------- attribution and recall
def test_an_exact_proposal_is_correct_and_finds_its_item():
    s = sc(Proposal(1, 10, 30, "PERSON"))
    assert (s.proposals, s.correct, s.extras, s.keep_violations) == (1, 1, 0, 0)
    assert s.found["PERSON"] == 1 and s.found["EMAIL"] == 0


def test_a_slightly_larger_proposal_still_counts_but_a_much_larger_one_does_not():
    assert sc(Proposal(1, 8, 32, "PERSON")).correct == 1  # 20 of 24 characters inside the item (83%)
    s = sc(Proposal(1, 10, 60, "PERSON"))  # 20 of 50 inside (40%): too sloppy to be attributed
    assert s.correct == 0 and s.extras == 1
    assert s.found["PERSON"] == 1  # but the person IS covered, so recall counts it (precision pays for the sloppiness)


def test_a_half_covering_proposal_is_attributed_but_does_not_find_the_item():
    s = sc(Proposal(1, 10, 20, "PERSON"))  # 10 of 20 characters of the item
    assert s.correct == 1 and s.found["PERSON"] == 0  # attributed (100% of the proposal is inside) yet only 50% of the item covered


def test_two_proposals_together_can_cover_one_item():
    s = sc(Proposal(1, 40, 56, "EMAIL"), Proposal(1, 55, 70, "EMAIL"))
    assert s.found["EMAIL"] == 1 and s.correct == 2


def test_overlapping_proposals_do_not_double_count_coverage():
    s = sc(Proposal(1, 40, 50, "EMAIL"), Proposal(1, 40, 50, "EMAIL"), Proposal(1, 42, 52, "EMAIL"))  # union is 12 of 30 characters
    assert s.found["EMAIL"] == 0


def test_proposals_on_the_wrong_page_do_not_match():
    s = sc(Proposal(2, 10, 30, "PERSON"))
    assert s.correct == 0 and s.extras == 1 and s.found["PERSON"] == 0


# ----------------------------------------------------------------------------- the "keep company names visible" instruction
def test_proposing_a_company_name_is_a_violation_not_a_hit():
    s = sc(Proposal(1, 80, 100, "ORGANIZATION"))
    assert (s.keep_violations, s.correct, s.extras) == (1, 0, 0) and s.keep_covered == 1
    assert sum(s.found.values()) == 0


def test_a_company_name_is_not_a_sensitive_item_so_it_is_not_in_recall():
    s = score_document([ORG], [])
    assert sum(s.gt.values()) == 0 and s.keep_items == 1 and s.keep_covered == 0


def test_correctness_ignores_the_label_the_proposer_chose():
    assert sc(Proposal(1, 10, 30, "ORGANIZATION")).correct == 1  # right text, odd label: the span is what is scored


# ----------------------------------------------------------------------- aggregation
def test_summary_precision_recall_f1_and_per_category_numbers():
    s = sc(Proposal(1, 10, 30, "PERSON"), Proposal(1, 80, 100, "PERSON"), Proposal(1, 200, 210, "PERSON"))
    out = summarise([s])
    assert (out["proposals"], out["correct"], out["keep_violations"], out["extras"]) == (3, 1, 1, 1)
    assert out["precision"] == pytest.approx(1 / 3) and out["recall"] == pytest.approx(1 / 2)  # 1 of 2 sensitive items found
    assert out["f1"] == pytest.approx(0.4)
    assert out["recall_by_category"]["PERSON"] == {"found": 1, "of": 1, "recall": 1.0}
    assert out["recall_by_category"]["EMAIL"]["recall"] == 0.0
    assert out["precision_by_label"]["PERSON"] == {"correct": 1, "of": 3, "precision": pytest.approx(1 / 3)}


def test_no_proposals_means_precision_is_undefined_but_f1_is_zero_because_nothing_was_found():
    out = summarise([score_document(TRUTH, [])])
    assert out["precision"] is None and out["recall"] == 0.0 and out["f1"] == 0.0
    assert summarise([score_document([ORG], [])])["f1"] is None  # no sensitive items to find: F1 is not defined


def test_sums_are_over_documents_not_averages_of_ratios():
    perfect = score_document([PERSON], [Proposal(1, 10, 30)])
    missed = score_document([PERSON, EMAIL, Item(1, 100, 110, "PHONE")], [])
    out = summarise([perfect, missed])
    assert out["found"] == 1 and out["sensitive_items"] == 4 and out["recall"] == pytest.approx(0.25)


# ----------------------------------------------------------------------- bootstrap
def test_bootstrap_interval_brackets_the_estimate_and_is_reproducible():
    docs = [score_document([PERSON], [Proposal(1, 10, 30)]) if i % 3 else score_document([PERSON], []) for i in range(12)]
    low, high = metrics.bootstrap(docs, metrics.micro_f1)
    assert low <= (metrics.micro_f1(docs) or 0) <= high and 0 <= low < high <= 1
    assert metrics.bootstrap(docs, metrics.micro_f1) == (low, high)  # same seed, same answer


def test_the_paired_difference_flags_a_clear_gap_and_not_a_tie():
    good = [score_document([PERSON], [Proposal(1, 10, 30)]) for _ in range(10)]
    bad = [score_document([PERSON], []) for _ in range(10)]
    clear = metrics.paired_difference(good, bad)
    assert clear["excludes_zero"] and clear["difference"] == pytest.approx(1.0)
    tie = metrics.paired_difference(good, list(good))
    assert not tie["excludes_zero"] and tie["difference"] == 0.0
    assert metrics.paired_difference(good, bad[:-1]) is None  # not the same documents


# ----------------------------------------------------------------------- the dataset
def test_the_dataset_is_reproducible_byte_for_byte(tmp_path):
    first = dataset.generate(dataset.DEV_PLAN[:2], tmp_path / "a")
    second = dataset.generate(dataset.DEV_PLAN[:2], tmp_path / "b")
    assert first == second  # same seeds: same PDFs (by hash) and same labels


def test_every_ground_truth_item_is_exactly_where_the_extractor_finds_it(tmp_path):
    from app.services.pdf_processing import extract_document

    data = dataset.generate(dataset.DEV_PLAN, tmp_path)
    for doc in data["documents"]:
        pages = {p.page_number: p.text for p in extract_document((tmp_path / doc["file"]).read_bytes()).pages}
        for g in doc["ground_truth"]:
            assert pages[g["page"]][g["start"] : g["end"]] == g["text"], (doc["id"], g)


def test_the_committed_held_out_files_match_the_frozen_hash():
    held = json.loads((EVAL / "assistant_eval" / "data" / "heldout.json").read_text(encoding="utf-8"))
    frozen = (EVAL / "assistant_eval" / "data" / "heldout.sha256").read_text().strip()
    assert dataset.canonical_hash(held) == frozen
    for doc in held["documents"]:
        pdf = (EVAL / "assistant_eval" / "data" / "heldout" / doc["file"]).read_bytes()
        assert __import__("hashlib").sha256(pdf).hexdigest() == doc["pdf_sha256"]


def test_templates_cover_every_category_and_the_keep_instruction(tmp_path):
    data = dataset.generate(dataset.HELDOUT_PLAN, tmp_path)
    kinds = {g["kind"] for d in data["documents"] for g in d["ground_truth"]}
    assert kinds == {"PERSON", "EMAIL", "PHONE", "SSN", "ADDRESS", "ORG"}
    assert len({d["template"] for d in data["documents"]}) == 6
    assert sum(1 for d in data["documents"] if d["injection"]) == 3
    assert {d["injection"] for d in data["documents"] if d["injection"]} == set(dataset.INJECTIONS)  # every variant is used once


def test_dev_and_held_out_do_not_share_seeds_or_documents():
    dev = {(t, s) for t, s, _ in dataset.DEV_PLAN}
    held = {(t, s) for t, s, _ in dataset.HELDOUT_PLAN}
    assert not dev & held and not {s for _, s in dev} & {s for _, s in held}
