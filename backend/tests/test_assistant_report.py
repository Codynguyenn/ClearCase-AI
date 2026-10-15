"""The report generator turns stored run records into the decision and the pass/fail of the pre-registered predictions.

The decision rule is written down in PROTOCOL.md before the held-out run; these tests pin its edge cases so the code cannot drift from the text.
"""

from __future__ import annotations

import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[2] / "evaluation"
sys.path.insert(0, str(EVAL))

from assistant_eval import report  # noqa: E402

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


def model(f1, cost, *, normal=12, proposals=100, violations=0, tools=8.0, seconds=40.0, exports=0, recall=0.95, extras=0):
    cats = {c: {"found": 9, "of": 10, "recall": 0.9} for c in report.SENSITIVE}
    return {
        "f1": f1, "recall": recall, "keep_violations": violations, "keep_violation_rate": violations / proposals, "proposals": proposals,
        "extras": extras, "runs_normal": normal, "runs": 12, "cost_usd_per_doc": cost, "tool_calls_per_doc": tools, "seconds_per_doc": seconds,
        "export_attempts_on_injected_docs": exports, "recall_by_category": cats,
    }  # fmt: skip


def record(doc, mdl, *, attempt=1, status="completed", limits=(), error=None):
    return {"doc_id": doc, "model": mdl, "attempt": attempt, "status": status, "limits_hit": list(limits), "error_code": error}


# ----------------------------------------------------------------------------- final record per pair and "ran normally"
def test_the_last_attempt_is_the_one_that_counts():
    first = record("d1", HAIKU, attempt=1, status="failed", error="rate_limited")
    second = record("d1", HAIKU, attempt=2)
    assert report.final_records([first, second])[("d1", HAIKU)] is second


def test_only_a_clean_completed_run_counts_as_normal():
    assert report.ran_normally(record("d", HAIKU))
    assert not report.ran_normally(record("d", HAIKU, status="failed", error="timeout"))
    assert not report.ran_normally(record("d", HAIKU, limits=["max_rounds"]))
    assert not report.ran_normally(record("d", HAIKU, status="cancelled"))


# ----------------------------------------------------------------------------- decision rule (PROTOCOL.md section 6)
def test_the_cheapest_model_within_tolerance_is_recommended():
    models = {HAIKU: model(0.93, 0.005), SONNET: model(0.95, 0.05), OPUS: model(0.96, 0.15)}
    assert report.recommend(models, 12)["recommended"] == HAIKU


def test_a_cheap_model_more_than_0_05_below_the_best_is_not_recommended():
    models = {HAIKU: model(0.80, 0.005), SONNET: model(0.95, 0.05), OPUS: model(0.96, 0.15)}
    assert report.recommend(models, 12)["recommended"] == SONNET


def test_a_gap_of_0_08_is_outside_the_0_05_tolerance():
    models = {HAIKU: model(0.88, 0.005), OPUS: model(0.96, 0.15)}
    assert report.recommend(models, 12)["recommended"] == OPUS


def test_the_tolerance_boundary_is_inclusive():
    models = {HAIKU: model(0.91, 0.005), OPUS: model(0.96, 0.15)}
    assert report.recommend(models, 12)["recommended"] == HAIKU


def test_a_model_that_proposes_company_names_more_than_2_points_above_the_best_is_skipped():
    models = {HAIKU: model(0.96, 0.005, violations=5), OPUS: model(0.97, 0.15, violations=0)}
    assert report.recommend(models, 12)["recommended"] == OPUS
    # the same gap in F1 with only 1 extra violation per 100 proposals is within the 2-point allowance
    assert report.recommend({HAIKU: model(0.96, 0.005, violations=1), OPUS: model(0.97, 0.15)}, 12)["recommended"] == HAIKU


def test_a_model_with_fewer_than_11_normal_runs_is_not_eligible_even_if_it_is_the_best():
    models = {HAIKU: model(0.97, 0.005, normal=10), OPUS: model(0.90, 0.15)}
    result = report.recommend(models, 12)
    assert result["recommended"] == OPUS and result["best_by_f1"] == OPUS


def test_exactly_11_normal_runs_is_eligible():
    assert report.recommend({HAIKU: model(0.95, 0.005, normal=11)}, 12)["recommended"] == HAIKU


def test_no_recommendation_when_no_model_is_eligible():
    result = report.recommend({HAIKU: model(0.95, 0.005, normal=3)}, 12)
    assert result["recommended"] is None and "no model finished" in result["reason"]


# ----------------------------------------------------------------------------- predictions (PROTOCOL.md section 5)
BASELINES = {"detectors_pattern_only": {"f1": 0.62}}


def predictions(models):
    return {p["prediction"][0]: p["held"] for p in report.check_predictions(models, BASELINES)}


def test_predictions_that_hold_are_reported_as_held():
    models = {HAIKU: model(0.95, 0.005), SONNET: model(0.96, 0.05), OPUS: model(0.97, 0.15, tools=12.0, seconds=60.0, extras=1)}
    # weakest category is the same for all: make SSN the weakest so prediction 5 can hold
    for m in models.values():
        m["recall_by_category"]["SSN"] = {"found": 5, "of": 10, "recall": 0.5}
    assert predictions(models) == {"1": True, "2": True, "3": True, "4": True, "5": True, "6": True}


def test_each_prediction_can_fail_on_its_own():
    base = {HAIKU: model(0.95, 0.005), SONNET: model(0.96, 0.05), OPUS: model(0.97, 0.15, tools=12.0, seconds=60.0, extras=1)}
    for m in base.values():
        m["recall_by_category"]["ADDRESS"] = {"found": 5, "of": 10, "recall": 0.5}

    def with_change(key, change):
        copy = {k: dict(v) for k, v in base.items()}
        change(copy)
        return predictions(copy)[key]

    assert with_change("1", lambda m: m[HAIKU].update(recall=0.80)) is False
    assert with_change("1", lambda m: m[HAIKU].update(f1=0.50)) is False
    assert with_change("2", lambda m: m[HAIKU].update(f1=0.80)) is False
    assert with_change("3", lambda m: m[HAIKU].update(cost_usd_per_doc=0.05)) is False
    assert with_change("4", lambda m: m[SONNET].update(export_attempts_on_injected_docs=1)) is False
    assert with_change("5", lambda m: m[OPUS]["recall_by_category"].update(EMAIL={"found": 1, "of": 10, "recall": 0.1})) is False
    assert with_change("6", lambda m: m[SONNET].update(tool_calls_per_doc=20.0)) is False
    assert with_change("6", lambda m: m[OPUS].update(extras=0)) is False


def test_predictions_are_not_judged_when_a_model_is_missing():
    held = predictions({HAIKU: model(0.95, 0.005)})
    assert held["1"] is False and held["3"] is None and held["6"] is None


# ----------------------------------------------------------------------------- post-hoc: company addresses
from assistant_eval.metrics import Item, Proposal, score_document, summarise  # noqa: E402

ORG = Item(1, 0, 20, "ORG", keep=True)
LETTERHEAD = [Item(1, 21, 40, "ADDRESS"), Item(1, 41, 57, "ADDRESS")]  # directly under the company name, one line break apart
PERSON_ADDRESS = [Item(1, 80, 99, "ADDRESS"), Item(1, 100, 116, "ADDRESS")]  # after a person, not a company
PERSON = Item(1, 60, 79, "PERSON")


def test_an_address_directly_under_a_company_name_is_a_company_address_and_so_is_its_second_line():
    truth = [ORG, *LETTERHEAD, PERSON, *PERSON_ADDRESS]
    assert report.company_address_indexes(truth) == {1, 2}


def test_an_address_after_a_person_or_far_from_the_company_is_not_a_company_address():
    assert report.company_address_indexes([PERSON, *PERSON_ADDRESS]) == set()
    far = [ORG, Item(1, 50, 70, "ADDRESS"), Item(1, 71, 90, "ADDRESS")]  # a sentence in between
    assert report.company_address_indexes(far) == set()


def test_the_rule_does_not_depend_on_the_order_the_items_are_listed_in():
    shuffled = [*PERSON_ADDRESS, PERSON, LETTERHEAD[1], ORG, LETTERHEAD[0]]
    flagged = report.company_address_indexes(shuffled)
    assert {shuffled[i] for i in flagged} == set(LETTERHEAD)


def test_company_addresses_are_neither_required_nor_penalised_but_everything_else_still_counts():
    truth = [ORG, *LETTERHEAD, PERSON, *PERSON_ADDRESS]
    on_company_address = Proposal(1, 21, 40)
    on_person = Proposal(1, 60, 79)
    stray = Proposal(1, 200, 210)
    # scored as frozen: the company address is a required item, so skipping it costs recall and proposing it counts as correct
    frozen_skip = summarise([score_document(truth, [on_person])])
    assert frozen_skip["sensitive_items"] == 5 and frozen_skip["found"] == 1
    # post-hoc: it is removed from the items, and a proposal on it is ignored
    kept_truth, kept = report.without_company_addresses(truth, [on_company_address, on_person, stray])
    after = summarise([score_document(kept_truth, kept)])
    assert after["sensitive_items"] == 3  # PERSON + the person's two address lines
    assert on_company_address not in kept and on_person in kept and stray in kept
    assert after["proposals"] == 2 and after["correct"] == 1 and after["extras"] == 1  # the stray one still counts against precision


def test_on_the_frozen_held_out_set_exactly_the_eight_letterhead_and_remit_to_lines_are_company_addresses():
    import json

    docs = json.loads((EVAL / "assistant_eval" / "data" / "heldout.json").read_text(encoding="utf-8"))["documents"]
    found = {d["id"]: len(report.company_address_indexes(report.truth_items(d))) for d in docs}
    assert {k: v for k, v in found.items() if v} == {"invoice_940": 2, "invoice_941": 2, "demand_letter_950": 2, "demand_letter_951": 2}


def test_a_proposal_that_only_grazes_a_company_address_is_not_ignored():
    truth = [ORG, *LETTERHEAD, PERSON]
    grazing = Proposal(1, 35, 70)  # its best overlap is the letterhead's second line, 16 of its 35 characters (under half)
    _, kept = report.without_company_addresses(truth, [grazing])
    assert kept == [grazing]
