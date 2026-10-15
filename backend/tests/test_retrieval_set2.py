"""The second held-out retrieval set: the tests that keep its construction honest.

The set is only worth having if the writer really never saw the policies, if nothing is spent before the protocol records the exact
prompts, and if a question is kept only when two labelers agree. Each rule has a small hand-made case.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[2] / "evaluation"
sys.path.insert(0, str(EVAL))

from retrieval2 import build  # noqa: E402
from retrieval2.build import Label, Passage  # noqa: E402

CORPUS = [
    Passage(1, "red", "1. Names", "text one"),
    Passage(2, "red", "2. Emails", "text two"),
    Passage(3, "share", "1. Approval", "text three"),
]


# ----------------------------------------------------------------------------- the writer is blind
def test_the_writer_prompt_contains_no_policy_text_or_section_names():
    corpus = build.load_corpus()
    assert len(corpus) == 19
    prompt = (build.WRITER_SYSTEM + " " + build.WRITER_USER + " " + " ".join(build.PERSONAS)).lower()
    prompt_words = re.findall(r"[a-z0-9']+", prompt)
    prompt_grams = {" ".join(prompt_words[i : i + 5]) for i in range(len(prompt_words) - 4)}
    for p in corpus:
        text_words = re.findall(r"[a-z0-9']+", p.text.lower())
        grams = {" ".join(text_words[i : i + 5]) for i in range(len(text_words) - 4)}
        assert not (grams & prompt_grams), f"the writer prompt repeats five words of the policy text ({p.policy_id}: {p.section})"
        heading = re.sub(r"^\d+\.\s*", "", p.section).strip().lower()
        if len(heading.split()) >= 2 and heading not in ("purpose and scope",):
            assert heading not in prompt, f"the writer prompt contains the section name '{p.section}'"


def test_the_labeler_prompt_is_the_only_place_the_passages_are_shown():
    block = build.passages_block(build.load_corpus())
    assert "{passages}" in build.LABELER_USER and block not in build.LABELER_USER
    assert "{passages}" not in build.WRITER_USER


# ----------------------------------------------------------------------------- the protocol comes first
def test_the_protocol_records_the_hash_of_the_exact_prompts():
    build.check_protocol_matches_prompts()  # does not raise
    text = build.PROTOCOL.read_text(encoding="utf-8")
    for digest in build.prompt_hashes().values():
        assert digest in text


def test_spending_is_refused_if_the_protocol_lacks_a_prompt_hash(tmp_path, monkeypatch):
    stale = tmp_path / "PROTOCOL.md"
    stale.write_text("only the writer hash is here: " + build.prompt_hashes()["writer"], encoding="utf-8")
    monkeypatch.setattr(build, "PROTOCOL", stale)
    with pytest.raises(SystemExit, match="REFUSING TO SPEND"):
        build.check_protocol_matches_prompts()
    monkeypatch.setattr(build, "PROTOCOL", tmp_path / "missing.md")
    with pytest.raises(SystemExit, match="REFUSING TO SPEND"):
        build.check_protocol_matches_prompts()


def test_changing_a_prompt_changes_its_hash(monkeypatch):
    before = build.prompt_hashes()
    monkeypatch.setattr(build, "WRITER_USER", build.WRITER_USER + " ")
    after = build.prompt_hashes()
    assert after["writer"] != before["writer"] and after["labeler"] == before["labeler"]


def test_the_expected_cost_is_a_few_cents_and_under_the_cap(capsys):
    build.stage_plan()
    out = capsys.readouterr().out
    expected = float(re.search(r"expected total about \$([\d.]+)", out).group(1))
    assert expected < 0.12 and expected < build.CAP_USD  # "a few cents"


def test_the_budget_refuses_a_call_that_could_pass_the_cap():
    budget = build.Budget(0.20)
    budget.add(0.18)
    assert budget.can_start(0.02) and not budget.can_start(0.021)


# ----------------------------------------------------------------------------- parsing
def test_parse_json_array_accepts_a_bare_array_or_a_fenced_one_and_nothing_else():
    assert build.parse_json_array('["a", "b"]') == ["a", "b"]
    assert build.parse_json_array('```json\n["a"]\n```') == ["a"]
    with pytest.raises(ValueError):
        build.parse_json_array("Sorry, I cannot do that.")
    with pytest.raises(json.JSONDecodeError):
        build.parse_json_array("[not json]")


def label(v, *p):
    return {"id": "q1", "v": v, "p": list(p)}


def test_parse_labels_keeps_only_valid_labels():
    ids = ["q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8"]
    items = [
        {"id": "q1", "v": "A", "p": [1, 2]},  # valid
        {"id": "q2", "v": "N", "p": []},  # valid
        {"id": "q3", "v": "A", "p": []},  # answerable with no passage: invalid
        {"id": "q4", "v": "A", "p": [99]},  # passage that does not exist
        {"id": "q5", "v": "Maybe", "p": []},  # unknown verdict
        {"id": "zzz", "v": "N", "p": []},  # unknown question
        {"id": "q1", "v": "N", "p": []},  # duplicate id: the first one stays
        {"id": "q6", "v": "A", "p": [True]},  # a boolean is not a passage number
        "not an object",
    ]
    labels = build.parse_labels(items, ids, n_passages=3)
    assert set(labels) == {"q1", "q2"}
    assert labels["q1"].verdict == "A" and labels["q1"].passages == (1, 2)


# ----------------------------------------------------------------------------- selection
def test_dedupe_drops_repeats_and_copies_of_earlier_questions_and_counts_them():
    existing = ["Can we share a redacted contract with an outside auditor?"]
    candidates = [
        {"q": "How long do we keep redacted copies of contracts?"},
        {"q": "How long should we keep the redacted copies of contracts?"},  # repeat of the first
        {"q": "Can we share a redacted contract with an outside auditor?"},  # copy of an earlier set
        {"q": ""},
        {"q": "x" * 401},
        {"q": "Who signs off on sending documents to a regulator?"},
    ]
    kept, dropped = build.dedupe(candidates, existing)
    assert [k["q"] for k in kept] == ["How long do we keep redacted copies of contracts?", "Who signs off on sending documents to a regulator?"]
    assert dropped == {"empty_or_too_long": 2, "repeat": 1, "copy_of_earlier_set": 1}


def combine(a, b, order=("q1",)):
    return build.combine(a, b, CORPUS, list(order))


def test_two_labelers_that_agree_on_not_addressed_make_an_unanswerable_question():
    kept, excluded = combine({"q1": Label("N", (), "")}, {"q1": Label("N", (), "")})
    assert kept == [{"id": "q1", "answerable": False, "relevant": []}] and sum(excluded.values()) == 0


def test_the_relevant_sections_are_the_intersection_of_what_both_labelers_named_not_the_union():
    kept, _ = combine({"q1": Label("A", (1, 2), "")}, {"q1": Label("A", (2, 3), "")})
    assert kept == [{"id": "q1", "answerable": True, "relevant": [["red", "2. Emails"]]}]


def test_answerable_with_no_common_passage_is_excluded():
    kept, excluded = combine({"q1": Label("A", (1,), "")}, {"q1": Label("A", (3,), "")})
    assert kept == [] and excluded["labelers_name_different_passages"] == 1


def test_disagreement_unclear_and_missing_labels_are_excluded_and_counted_separately():
    a = {"q1": Label("A", (1,), ""), "q2": Label("U", (1,), ""), "q3": Label("N", (), ""), "q4": Label("N", (), "")}
    b = {"q1": Label("N", (), ""), "q2": Label("A", (1,), ""), "q3": Label("U", (1,), "")}  # q4 has no second label
    kept, excluded = combine(a, b, order=("q1", "q2", "q3", "q4"))
    assert kept == []
    assert excluded == {"missing_label": 1, "unclear": 2, "labelers_disagree_on_verdict": 1, "labelers_name_different_passages": 0}


def test_two_policies_with_the_same_section_name_stay_distinct():
    corpus = [Passage(1, "p1", "Introduction", "x"), Passage(2, "p2", "Introduction", "y")]
    kept, _ = build.combine({"q": Label("A", (2,), "")}, {"q": Label("A", (2,), "")}, corpus, ["q"])
    assert kept[0]["relevant"] == [["p2", "Introduction"]]


# ----------------------------------------------------------------------------- freezing
def test_freeze_refuses_to_rebuild_a_set_that_is_already_frozen(tmp_path, monkeypatch):
    marker = tmp_path / "retrieval_heldout2.sha256"
    marker.write_text("abc\n")
    monkeypatch.setattr(build, "FROZEN_HASH", marker)
    with pytest.raises(SystemExit, match="Already frozen"):
        build.stage_freeze()


def test_the_committed_frozen_set_matches_its_hash_if_it_exists():
    if not build.FROZEN_HASH.exists():
        pytest.skip("not frozen yet")
    import hashlib

    assert hashlib.sha256(build.FROZEN.read_bytes()).hexdigest() == build.FROZEN_HASH.read_text().strip()


# ----------------------------------------------------------------------------- the report
from retrieval2 import report  # noqa: E402


def qrow(answerable, *, status="ok", rank=None, relevant_served=False, top=0.5, served=()):
    row = {"id": "x", "answerable": answerable, "status": status, "top_cosine": top, "served": list(served)}
    if answerable:
        row |= {"first_relevant_rank": rank, "relevant_served": relevant_served}
    return row


def test_the_does_not_address_passage_id_is_a_real_citation_id():
    """A misspelt id would make 'shown only that passage' silently zero; tie the constant to the real chunking."""
    from app.services.policy_chunking import chunk_markdown, parse_front_matter

    meta, body = parse_front_matter((build.POLICY_DIR / "fictional_redaction_policy.md").read_text(encoding="utf-8-sig"))
    chunk = next(c for c in chunk_markdown(body) if c.section.endswith("Topics this policy does not address"))
    assert report.NOT_ADDRESSED_PASSAGE == f"{meta['policy_id']}:{meta['version']}:{chunk.ordinal}"


def test_summarise_counts_each_outcome_from_hand_made_rows():
    rows = [
        qrow(True, rank=1, relevant_served=True),
        qrow(True, rank=3, status="no_relevant_policy"),
        qrow(True, rank=None, status="ok"),  # shown, but not the relevant passage
        qrow(False, status="no_relevant_policy", top=0.30),
        qrow(False, status="ok", top=0.60, served=[report.NOT_ADDRESSED_PASSAGE]),
        qrow(False, status="ok", top=0.45, served=["a:1.0:2", report.NOT_ADDRESSED_PASSAGE]),
    ]
    s = report.summarise(rows)
    assert (s["hit@1"]["k"], s["hit@3"]["k"], s["relevant_shown"]["k"], s["wrongly_refused"]["k"], s["shown_wrong"]["k"]) == (1, 2, 1, 1, 1)
    assert s["mrr"] == pytest.approx((1 + 1 / 3) / 3)
    assert (s["unanswerable_refused"]["k"], s["unanswerable_shown"]["k"]) == (1, 2)
    assert s["balanced_accuracy"] == pytest.approx((1 / 3 + 1 / 3) / 2)
    assert s["highest_unanswerable_top_cosine"] == 0.60 and s["margin"] == pytest.approx(0.38 - 0.60)
    assert s["unanswerable_shown_only_the_does_not_address_passage"] == 1  # the second one was also shown another passage
    assert s["does_not_address_passage_served_for"] == 2


def test_the_balanced_accuracy_interval_is_reproducible_and_brackets_the_estimate():
    rows = [qrow(True, relevant_served=i % 2 == 0) for i in range(20)] + [qrow(False, status="no_relevant_policy" if i % 4 else "ok") for i in range(20)]
    low, high = report.bootstrap_ba(rows)
    assert (low, high) == tuple(report.bootstrap_ba(rows))
    assert low < report.summarise(rows)["balanced_accuracy"] < high


def test_the_difference_interval_straddles_zero_for_equal_sets_and_excludes_it_for_clearly_different_ones():
    same = [qrow(True, relevant_served=i % 2 == 0) for i in range(30)] + [qrow(False, status="no_relevant_policy" if i % 2 else "ok") for i in range(30)]
    low, high = report.bootstrap_ba_difference(same, same)
    assert low < 0 < high
    great = [qrow(True, relevant_served=True) for _ in range(30)] + [qrow(False, status="no_relevant_policy") for _ in range(30)]
    bad = [qrow(True, relevant_served=False) for _ in range(30)] + [qrow(False, status="ok") for _ in range(30)]
    low, high = report.bootstrap_ba_difference(great, bad)
    assert low > 0.9


def test_each_prediction_is_evaluated_against_its_own_threshold():
    base = {"hit@3": {"value": 0.86, "k": 31, "n": 36}, "balanced_accuracy": 0.61, "wrongly_refused": {"value": 0.5, "k": 18, "n": 36}, "margin": -0.14}
    pooled = {"hit@3": {"k": 52, "n": 64, "low": 0.70, "high": 0.89}}
    held = lambda s, p: [x["held"] for x in report.predictions(s, p)]  # noqa: E731
    assert held(base, pooled) == [False, True, True, True, True]
    assert held({**base, "hit@3": {"value": 0.70, "k": 25, "n": 36}}, pooled)[0] is True
    assert held({**base, "balanced_accuracy": 0.70}, pooled)[1] is False  # "below 0.70" is strict
    assert held({**base, "wrongly_refused": {"value": 0.24, "k": 8, "n": 36}}, pooled)[2] is False
    assert held({**base, "margin": 0.01}, pooled)[3] is False
    assert held(base, {"hit@3": {"k": 60, "n": 64, "low": 0.80, "high": 0.90}})[4] is False  # "below 0.90" is strict
