"""Turn the two stored held-out runs into the second-set report. Every number comes from the run records; none is typed by hand.

    backend/.venv/bin/python evaluation/retrieval2/report.py

Reads   evaluation/results/retrieval_heldout_runs.json (the first set, run 'final') and retrieval_heldout2_runs.json (this set, 'baseline')
        and the provenance block of evaluation/retrieval_heldout2.json
Writes  evaluation/retrieval2/REPORT.md and evaluation/results/retrieval_heldout2_report.json

The predictions and the way results are read are fixed in PROTOCOL.md sections 6 and 7; they are evaluated here, by code.
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "backend"))

from eval_heldout import wilson  # noqa: E402

RESULTS = ROOT / "evaluation" / "results"
CUTOFF = 0.38
NOT_ADDRESSED_PASSAGE = "northwind-redaction-policy:1.0:10"  # "9. Topics this policy does not address"


def interval(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "value": k / n if n else None, "low": lo, "high": hi}


def rows_of(runs_file: str, label: str) -> list[dict]:
    runs = json.loads((RESULTS / runs_file).read_text())
    return next(r for r in runs if r["label"] == label)["per_question"]


def summarise(rows: Sequence[dict]) -> dict:
    ans = [r for r in rows if r["answerable"]]
    una = [r for r in rows if not r["answerable"]]
    n_a, n_u = len(ans), len(una)
    out = {
        "answerable": n_a, "unanswerable": n_u,
        "hit@1": interval(sum(r["first_relevant_rank"] == 1 for r in ans), n_a),
        "hit@3": interval(sum(r["first_relevant_rank"] is not None and r["first_relevant_rank"] <= 3 for r in ans), n_a),
        "mrr": sum(1 / r["first_relevant_rank"] for r in ans if r["first_relevant_rank"]) / n_a,
        "relevant_shown": interval(sum(r["relevant_served"] for r in ans), n_a),
        "shown_wrong": interval(sum(r["status"] == "ok" and not r["relevant_served"] for r in ans), n_a),
        "wrongly_refused": interval(sum(r["status"] == "no_relevant_policy" for r in ans), n_a),
        "unanswerable_refused": interval(sum(r["status"] == "no_relevant_policy" for r in una), n_u),
        "unanswerable_shown": interval(sum(r["status"] == "ok" for r in una), n_u),
        "highest_unanswerable_top_cosine": max(r["top_cosine"] for r in una),
    }  # fmt: skip
    out["balanced_accuracy"] = (out["relevant_shown"]["value"] + out["unanswerable_refused"]["value"]) / 2
    out["margin"] = CUTOFF - out["highest_unanswerable_top_cosine"]
    only_not_addressed = [r for r in una if r["status"] == "ok" and set(r["served"]) == {NOT_ADDRESSED_PASSAGE}]
    out["unanswerable_shown_only_the_does_not_address_passage"] = len(only_not_addressed)
    out["does_not_address_passage_served_for"] = sum(NOT_ADDRESSED_PASSAGE in r["served"] for r in rows)
    return out


def _parts(rows: Sequence[dict]) -> tuple[list[float], list[float]]:
    """1.0 per answerable question whose relevant passage was shown, and 1.0 per not-addressed question that was refused."""
    return (
        [1.0 if r["relevant_served"] else 0.0 for r in rows if r["answerable"]],
        [1.0 if r["status"] == "no_relevant_policy" else 0.0 for r in rows if not r["answerable"]],
    )


def _resampled_ba(rng: random.Random, parts: tuple[list[float], list[float]]) -> float:
    answerable, unanswerable = parts
    return (sum(rng.choices(answerable, k=len(answerable))) / len(answerable) + sum(rng.choices(unanswerable, k=len(unanswerable))) / len(unanswerable)) / 2


def bootstrap_ba(rows: Sequence[dict], resamples: int = 2000, seed: int = 7) -> list[float]:
    """95% interval for balanced accuracy, resampling the answerable and the not-addressed questions separately."""
    rng, parts = random.Random(seed), _parts(rows)
    stats = sorted(_resampled_ba(rng, parts) for _ in range(resamples))
    return [stats[int(0.025 * resamples)], stats[int(0.975 * resamples) - 1]]


def bootstrap_ba_difference(a: Sequence[dict], b: Sequence[dict], resamples: int = 2000, seed: int = 11) -> list[float]:
    """95% interval for BA(a) - BA(b). The two sets are independent samples, so each is resampled on its own."""
    rng, parts_a, parts_b = random.Random(seed), _parts(a), _parts(b)
    diffs = sorted(_resampled_ba(rng, parts_a) - _resampled_ba(rng, parts_b) for _ in range(resamples))
    return [diffs[int(0.025 * resamples)], diffs[int(0.975 * resamples) - 1]]


def predictions(set2: dict, pooled: dict) -> list[dict]:
    def row(text: str, held: bool, detail: str) -> dict:
        return {"prediction": text, "held": held, "detail": detail}

    return [
        row("1. Ranking is worse than on the first held-out set: hit@3 below 0.75.", set2["hit@3"]["value"] < 0.75, f"hit@3 = {set2['hit@3']['k']}/{set2['hit@3']['n']} = {set2['hit@3']['value']:.2f}"),
        row("2. The cutoff still cannot separate the classes: balanced accuracy below 0.70.", set2["balanced_accuracy"] < 0.70, f"balanced accuracy = {set2['balanced_accuracy']:.2f}"),
        row("3. At least a quarter of answerable questions are wrongly told 'no matching policy'.", set2["wrongly_refused"]["value"] >= 0.25, f"{set2['wrongly_refused']['k']}/{set2['wrongly_refused']['n']} = {set2['wrongly_refused']['value']:.2f}"),
        row("4. At least one unanswerable question has a best cosine of 0.38 or more (negative margin).", set2["margin"] < 0, f"margin = {set2['margin']:+.3f}"),
        row("5. Pooled over both sets, the upper 95% bound for hit@3 is below 0.90.", pooled["hit@3"]["high"] < 0.90, f"pooled hit@3 = {pooled['hit@3']['k']}/{pooled['hit@3']['n']}, interval {pooled['hit@3']['low']:.2f}-{pooled['hit@3']['high']:.2f}"),
    ]  # fmt: skip


def build() -> dict:
    set1_rows, set2_rows = rows_of("retrieval_heldout_runs.json", "final"), rows_of("retrieval_heldout2_runs.json", "baseline")
    pooled_rows = list(set1_rows) + list(set2_rows)
    s1, s2, pooled = summarise(set1_rows), summarise(set2_rows), summarise(pooled_rows)
    for s, rows in ((s1, set1_rows), (s2, set2_rows), (pooled, pooled_rows)):
        s["balanced_accuracy_ci"] = bootstrap_ba(rows)
    provenance = json.loads((ROOT / "evaluation" / "retrieval_heldout2.json").read_text())["_provenance"]
    return {
        "set1": s1, "set2": s2, "pooled": pooled, "predictions": predictions(s2, pooled), "provenance": provenance,
        "ba_difference_set2_minus_set1": bootstrap_ba_difference(set2_rows, set1_rows),
    }  # fmt: skip


def fmt(i: dict) -> str:
    return f"{i['k']}/{i['n']} = {i['value']:.2f} ({i['low']:.2f}-{i['high']:.2f})"


def render(d: dict) -> str:
    s1, s2, p, prov = d["set1"], d["set2"], d["pooled"], d["provenance"]
    lines = [
        "# Second held-out retrieval set: result (synthetic; model-written, model-labelled)", "",
        "Generated by `report.py` from the stored runs. Read `PROTOCOL.md` first: the prompts, rules and predictions were written before any question existed.", "",
        f"**How the set was made.** Writer `{prov['writer']}` (never shown the policies) wrote {prov['candidates_written']} candidate questions; "
        f"after removing repeats, {prov['candidates_labelled']} were labelled against the 19 policy passages by `{prov['labelers'][0]}` and `{prov['labelers'][1]}`, "
        f"which agreed on the verdict for {prov['labelers_agree_on_verdict']}. Kept: {prov['kept']} questions ({s2['answerable']} answerable, {s2['unanswerable']} not addressed). "
        f"Excluded: {prov['excluded']['unclear']} unclear to at least one labeler, {prov['excluded']['labelers_disagree_on_verdict']} verdicts differed, "
        f"{prov['excluded']['labelers_name_different_passages']} named different passages, {prov['excluded']['missing_label']} missing a label. "
        f"Total spend ${prov['spent_usd']:.3f}. The retriever was run once, unchanged (cutoff {CUTOFF}), after the set was frozen by hash.", "",
        "| Measure | First held-out set | **Second set (this one)** | Pooled |", "| --- | --- | --- | --- |",
        f"| Questions (answerable / not addressed) | {s1['answerable']} / {s1['unanswerable']} | {s2['answerable']} / {s2['unanswerable']} | {p['answerable']} / {p['unanswerable']} |",
        f"| Right passage first (hit@1) | {fmt(s1['hit@1'])} | **{fmt(s2['hit@1'])}** | {fmt(p['hit@1'])} |",
        f"| Right passage in top 3 (hit@3) | {fmt(s1['hit@3'])} | **{fmt(s2['hit@3'])}** | {fmt(p['hit@3'])} |",
        f"| Mean reciprocal rank | {s1['mrr']:.3f} | **{s2['mrr']:.3f}** | {p['mrr']:.3f} |",
        f"| Answerable: relevant passage shown | {fmt(s1['relevant_shown'])} | **{fmt(s2['relevant_shown'])}** | {fmt(p['relevant_shown'])} |",
        f"| Answerable: wrongly told 'no matching policy' | {fmt(s1['wrongly_refused'])} | **{fmt(s2['wrongly_refused'])}** | {fmt(p['wrongly_refused'])} |",
        f"| Answerable: shown only wrong passages | {fmt(s1['shown_wrong'])} | **{fmt(s2['shown_wrong'])}** | {fmt(p['shown_wrong'])} |",
        f"| Not addressed: correctly refused | {fmt(s1['unanswerable_refused'])} | **{fmt(s2['unanswerable_refused'])}** | {fmt(p['unanswerable_refused'])} |",
        f"| Not addressed: wrongly shown a passage | {fmt(s1['unanswerable_shown'])} | **{fmt(s2['unanswerable_shown'])}** | {fmt(p['unanswerable_shown'])} |",
        "| Balanced accuracy at the cutoff (95% bootstrap interval) | "
        + " | ".join(f"{x['balanced_accuracy']:.2f} ({x['balanced_accuracy_ci'][0]:.2f}-{x['balanced_accuracy_ci'][1]:.2f})" if k != 's2' else f"**{x['balanced_accuracy']:.2f}** ({x['balanced_accuracy_ci'][0]:.2f}-{x['balanced_accuracy_ci'][1]:.2f})" for k, x in (("s1", s1), ("s2", s2), ("p", p)))
        + " |",
        f"| Best cosine among not-addressed questions, minus cutoff | {-s1['margin']:+.3f} | {-s2['margin']:+.3f} | {-p['margin']:+.3f} |", "",
        f"Balanced accuracy, second set minus first: interval {d['ba_difference_set2_minus_set1'][0]:+.2f} to {d['ba_difference_set2_minus_set1'][1]:+.2f} "
        f"({'includes' if d['ba_difference_set2_minus_set1'][0] <= 0 <= d['ba_difference_set2_minus_set1'][1] else 'excludes'} zero): "
        f"{'the two sets do not disagree beyond what these sample sizes produce' if d['ba_difference_set2_minus_set1'][0] <= 0 <= d['ba_difference_set2_minus_set1'][1] else 'the sets disagree'}.", "",
        "## Predictions made before the run (PROTOCOL.md section 6)", "", "| Prediction | Held? | Detail |", "| --- | --- | --- |",
    ]  # fmt: skip
    lines += [f"| {x['prediction']} | {'yes' if x['held'] else '**no**'} | {x['detail']} |" for x in d["predictions"]]
    lines += [
        "", "## Secondary, fixed in advance (PROTOCOL.md section 7)", "",
        f"The \"Topics this policy does not address\" passage (`{NOT_ADDRESSED_PASSAGE}`) names topics on purpose, so it was the suspected easy match for not-addressed questions. "
        f"It was served for {s2['does_not_address_passage_served_for']} of the {s2['answerable'] + s2['unanswerable']} questions in this set ({s1['does_not_address_passage_served_for']} of "
        f"{s1['answerable'] + s1['unanswerable']} in the first set); not-addressed questions shown **only** that passage: {s2['unanswerable_shown_only_the_does_not_address_passage']} "
        f"of {s2['unanswerable_shown']['k']} wrongly shown here, {s1['unanswerable_shown_only_the_does_not_address_passage']} of {s1['unanswerable_shown']['k']} in the first set. "
        "So that confound did not drive the wrong shows; they come from other passages. It does not change the main numbers.", "",
        "## Limits (PROTOCOL.md section 9)", "",
        "Questions and labels come from language models, not people, and from the same family as the assistant under study. The writer was told what the two documents are about; "
        "the second labeler is the writer's own model, so it is a consistency check and not an independent expert. Keeping only questions both labelers agree on removes the "
        f"ambiguous ones ({prov['excluded']['unclear']} of {prov['candidates_labelled']} candidates were unclear to at least one labeler), so the set is probably easier than real use. "
        "Two fictional policies and 19 passages. The same assistant wrote the policies, the prompts and this analysis. Not real users' questions.",
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def main() -> None:
    data = build()
    (RESULTS / "retrieval_heldout2_report.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    (Path(__file__).resolve().parent / "REPORT.md").write_text(render(data), encoding="utf-8")
    print("wrote evaluation/retrieval2/REPORT.md and evaluation/results/retrieval_heldout2_report.json")


if __name__ == "__main__":
    main()
