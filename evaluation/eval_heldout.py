"""Run the FROZEN held-out question set through the production policy retriever.

    backend/.venv/bin/python evaluation/eval_heldout.py --label baseline
    backend/.venv/bin/python evaluation/eval_heldout.py --label final
    backend/.venv/bin/python evaluation/eval_heldout.py --set 2 --label baseline   # the second, more independent set (retrieval2/PROTOCOL.md)

Read evaluation/README.md ("Held-out protocol") first. In short:

* The question file must match the SHA-256 in retrieval_heldout.sha256 (retrieval_heldout2.sha256 for --set 2),
  otherwise this refuses to run (so a question cannot be edited after seeing results without it being visible).
* Each label may be used once. Every run is appended to results/retrieval_heldout_runs.json
  together with the configuration and a code fingerprint, so the full history is auditable.
* It runs the real ``MongoCosineRetriever`` (not a copy of its logic) against a scratch
  MongoDB database seeded with the two sample policies, so it measures what the app does.
* By default only aggregate numbers are printed. Per-question results are saved in the
  results file; do not use them to choose changes.

Nothing here is real-world accuracy: the questions are synthetic and same-author.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.db import Mongo  # noqa: E402
from app.services.policy_service import PolicyService  # noqa: E402
from app.services.retrieval import MongoCosineRetriever  # noqa: E402

QUESTIONS = ROOT / "evaluation" / "retrieval_heldout.json"
DEV_QUESTIONS = ROOT / "evaluation" / "retrieval_dev.json"
FROZEN_HASH = ROOT / "evaluation" / "retrieval_heldout.sha256"
RUNS = ROOT / "evaluation" / "results" / "retrieval_heldout_runs.json"
# The second held-out set (written blind to the policies, labelled by two models): same runner, its own file, hash and run history.
QUESTIONS2 = ROOT / "evaluation" / "retrieval_heldout2.json"
FROZEN_HASH2 = ROOT / "evaluation" / "retrieval_heldout2.sha256"
RUNS2 = ROOT / "evaluation" / "results" / "retrieval_heldout2_runs.json"
SCRATCH_DB = "clearcase_eval"
OWNER = "eval-user"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k/n (honest about small samples)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def prop(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "value": round(k / n, 4) if n else None, "ci95": [round(lo, 3), round(hi, 3)]}


def fmt(p: dict) -> str:
    return f"{p['k']}/{p['n']} = {p['value']:.2f}  (95% CI {p['ci95'][0]:.2f}-{p['ci95'][1]:.2f})"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_state() -> dict:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    return {"commit": run("rev-parse", "--short", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", type=int, choices=[1, 2], default=1, help="which frozen held-out set (default 1)")
    parser.add_argument("--label", help="e.g. baseline or final; each label may be used once")
    parser.add_argument("--smoke", action="store_true", help="test this script on the DEV set; nothing is recorded")
    parser.add_argument("--details", action="store_true", help="also print per-question results (do not tune on them)")
    parser.add_argument("--allow-repeat", action="store_true", help="re-use a label; the run is flagged as a repeat")
    args = parser.parse_args()

    if not args.smoke and not args.label:
        parser.error("--label is required (or use --smoke)")
    questions_path = DEV_QUESTIONS if args.smoke else (QUESTIONS2 if args.set == 2 else QUESTIONS)
    frozen_path, runs_path = (FROZEN_HASH2, RUNS2) if args.set == 2 else (FROZEN_HASH, RUNS)
    args.label = args.label or "smoke"

    # ---- 1. the question file must be the frozen one
    frozen = frozen_path.read_text().strip()
    actual = sha256(questions_path)
    if not args.smoke and actual != frozen:
        print(f"REFUSING TO RUN: {questions_path.name} changed.\n  frozen {frozen}\n  actual {actual}", file=sys.stderr)
        return 2

    # ---- 2. one run per label
    runs = json.loads(runs_path.read_text()) if runs_path.exists() else []
    repeat = any(r["label"] == args.label for r in runs)
    if repeat and not args.allow_repeat and not args.smoke:
        print(f"REFUSING TO RUN: label '{args.label}' was already used. See {runs_path.name}.", file=sys.stderr)
        return 2

    data = json.loads(questions_path.read_text())
    settings = Settings(_env_file=None, mongodb_db=SCRATCH_DB)

    # ---- 3. scratch database seeded with the two sample policies (never touches your real data)
    mongo = Mongo(settings)
    mongo.client.drop_database(SCRATCH_DB)
    mongo.ensure_indexes()
    try:
        service = PolicyService(mongo, settings)
        records = service.seed_samples(OWNER, Path(settings.sample_data_dir) / "policies")
        chunk_total = sum(r.chunk_count for r in records)

        production = MongoCosineRetriever(mongo, settings)
        # Same code, same data, but with the cutoff off and a large top_k, to see the full ranking.
        full_view = MongoCosineRetriever(
            mongo, settings.model_copy(update={"retrieval_min_score": -1.0, "retrieval_top_k_max": 100})
        )

        def relevant_set(item: dict) -> set[tuple[str, str]]:
            return {(p, s) for p, s in item["relevant"]}

        per_question = []
        for item in data["answerable"] + data["unanswerable"]:
            q = item["q"]
            answerable = "relevant" in item
            ranking = full_view.retrieve(OWNER, q, top_k=100).passages  # every chunk, production ordering, cosine scores
            served = production.retrieve(OWNER, q, top_k=3)  # exactly what the app would show
            # Integrity check: the app's answer must equal "full ranking, cut at the cutoff, first 3".
            expected = [p.citation_id for p in ranking if p.score >= settings.retrieval_min_score][:3]
            assert [p.citation_id for p in served.passages] == expected, f"production/full-ranking mismatch for {item['id']}"

            row = {
                "id": item["id"],
                "answerable": answerable,
                "status": served.status.value,
                "top_cosine": round(max(p.score for p in ranking), 4),
                "served": [p.citation_id for p in served.passages],
            }
            if answerable:
                rel = relevant_set(item)
                pos = next((i for i, p in enumerate(ranking, start=1) if (p.policy_id, p.section) in rel), None)
                row["first_relevant_rank"] = pos
                row["relevant_served"] = any((p.policy_id, p.section) in rel for p in served.passages)
                row["top1_is_relevant"] = bool(pos == 1)
                weak = getattr(served, "weak_passages", None)
                if weak is not None:
                    row["relevant_in_weak_tier"] = any((p.policy_id, p.section) in rel for p in weak)
            else:
                weak = getattr(served, "weak_passages", None)
                if weak is not None:
                    row["weak_tier_shown"] = len(weak) > 0
            per_question.append(row)
    finally:
        mongo.client.drop_database(SCRATCH_DB)
        mongo.close()

    ans = [r for r in per_question if r["answerable"]]
    una = [r for r in per_question if not r["answerable"]]
    n_a, n_u = len(ans), len(una)

    metrics = {
        "ranking_independent_of_cutoff": {
            "hit@1": prop(sum(r["first_relevant_rank"] == 1 for r in ans), n_a),
            "hit@3": prop(sum(r["first_relevant_rank"] is not None and r["first_relevant_rank"] <= 3 for r in ans), n_a),
            "mrr": round(sum(1 / r["first_relevant_rank"] for r in ans if r["first_relevant_rank"]) / n_a, 4),
        },
        "decisions_at_production_cutoff": {
            "answerable_relevant_passage_shown": prop(sum(r["relevant_served"] for r in ans), n_a),
            "answerable_shown_but_wrong_passages": prop(sum(r["status"] == "ok" and not r["relevant_served"] for r in ans), n_a),
            "answerable_wrongly_told_no_policy": prop(sum(r["status"] == "no_relevant_policy" for r in ans), n_a),
            "unanswerable_correctly_refused": prop(sum(r["status"] == "no_relevant_policy" for r in una), n_u),
            "unanswerable_wrongly_shown_a_passage": prop(sum(r["status"] == "ok" for r in una), n_u),
        },
        "highest_unanswerable_top_cosine": max(r["top_cosine"] for r in una),
        "margin_cutoff_minus_highest_unanswerable": round(settings.retrieval_min_score - max(r["top_cosine"] for r in una), 4),
    }
    d = metrics["decisions_at_production_cutoff"]
    metrics["balanced_accuracy"] = round((d["answerable_relevant_passage_shown"]["value"] + d["unanswerable_correctly_refused"]["value"]) / 2, 4)
    if any("relevant_in_weak_tier" in r for r in ans):
        refused = [r for r in ans if r["status"] == "no_relevant_policy"]
        metrics["weak_tier"] = {
            "recovers_relevant_among_wrongly_refused": prop(sum(r.get("relevant_in_weak_tier", False) for r in refused), len(refused)),
            "weak_passages_shown_for_unanswerable": prop(sum(r.get("weak_tier_shown", False) for r in una), n_u),
        }

    record = {
        "label": args.label,
        "repeat_of_earlier_label": repeat,
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": git_state(),
        "heldout_sha256": actual,
        "config": {
            "embedding_model": settings.embedding_model,
            "embedding_revision": settings.embedding_revision,
            "retrieval_min_score": settings.retrieval_min_score,
            "retrieval_keyword_weight": settings.retrieval_keyword_weight,
            "weak_min_score": getattr(settings, "retrieval_weak_min_score", None),
        },
        "code_fingerprint": {
            f: sha256(ROOT / "backend" / "app" / f)[:16] for f in ("services/retrieval.py", "services/embeddings.py", "config.py")
        },
        "corpus": {"policies": [f"{r.policy_id}:{r.version}" for r in records], "chunks": chunk_total},
        "questions": {"answerable": n_a, "unanswerable": n_u},
        "set": args.set,
        "note": (
            "Synthetic questions written by a model that had not seen the policies and labelled by two models: not real-world accuracy."
            if args.set == 2
            else "Synthetic, same-author questions: not real-world accuracy."
        ),
        "metrics": metrics,
        "per_question": per_question,
    }
    if not args.smoke:
        runs_path.parent.mkdir(exist_ok=True)
        runs_path.write_text(json.dumps([*runs, record], indent=2))

    # ---- 4. print aggregates (per-question only on request)
    r = metrics["ranking_independent_of_cutoff"]
    print(f"label: {args.label}   model: {settings.embedding_model.split('/')[-1]}   cutoff: {settings.retrieval_min_score}   git: {record['git']}")
    print(f"questions: {n_a} answerable, {n_u} unanswerable over {chunk_total} chunks\n")
    print("Ranking (independent of the cutoff):")
    print(f"  right passage first (hit@1): {fmt(r['hit@1'])}")
    print(f"  right passage in top 3     : {fmt(r['hit@3'])}")
    print(f"  mean reciprocal rank       : {r['mrr']:.3f}")
    print("\nDecisions at the production cutoff:")
    print(f"  answerable, relevant passage shown : {fmt(d['answerable_relevant_passage_shown'])}")
    print(f"  answerable, shown but wrong passage: {fmt(d['answerable_shown_but_wrong_passages'])}")
    print(f"  answerable, wrongly told 'no policy': {fmt(d['answerable_wrongly_told_no_policy'])}")
    print(f"  unanswerable, correctly refused    : {fmt(d['unanswerable_correctly_refused'])}")
    print(f"  unanswerable, wrongly shown passage: {fmt(d['unanswerable_wrongly_shown_a_passage'])}")
    print(f"  balanced accuracy: {metrics['balanced_accuracy']:.2f}   margin to best unanswerable: {metrics['margin_cutoff_minus_highest_unanswerable']:+.3f}")
    if "weak_tier" in metrics:
        w = metrics["weak_tier"]
        print("\nWeak 'possibly related' tier (human reviewer only):")
        print(f"  recovers a relevant passage for wrongly refused answerable: {fmt(w['recovers_relevant_among_wrongly_refused'])}")
        print(f"  shows weak passages for unanswerable questions            : {fmt(w['weak_passages_shown_for_unanswerable'])}")
    if args.details:
        print("\nPer question:")
        for row in per_question:
            print("  ", {k: v for k, v in row.items() if k != "served"})
    if args.smoke:
        print("\n(smoke test on the DEV set; nothing recorded)")
    else:
        print(f"\nrecorded in {runs_path.relative_to(ROOT)} (run {len(runs) + 1})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
