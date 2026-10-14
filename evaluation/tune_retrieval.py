"""Measure policy retrieval on the DEVELOPMENT question set and choose a similarity cutoff.

Run from the repository root:

    backend/.venv/bin/python evaluation/tune_retrieval.py

What it does (no database needed):
  1. Chunks the sample policies with the same chunker the app uses.
  2. Embeds every chunk and every question with the real local model.
  3. Reports hit@1, hit@3 and MRR on the answerable questions.
  4. Sweeps a cutoff on the best score and reports, for each value, how many
     answerable questions would still get a relevant passage and how many
     unanswerable questions would correctly get "the policy does not address this".
  5. Compares plain semantic ranking with semantic + keyword overlap. The production
     method is: order by semantic + 0.15 * keyword overlap, but only show passages
     whose plain semantic cosine reaches the cutoff (so the cutoff stays an
     interpretable cosine value).
  6. Writes everything, with the model revision and denominators, to
     evaluation/results/retrieval_dev.json.

This is a DEV set written by the same author as the policies (see the _about
field in retrieval_dev.json). The numbers below describe synthetic data only and
are not real-world accuracy. The held-out evaluation belongs to Phase 6.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402

from app.config import Settings  # noqa: E402
from app.services.embeddings import get_embedder  # noqa: E402
from app.services.policy_chunking import chunk_markdown, parse_front_matter  # noqa: E402
from app.services.policy_service import embedding_text  # noqa: E402

STOP = set("the and for with that this what when how are does can should about have from your you our any not but they them there their".split())
KEYWORD_WEIGHT = 0.15  # only used for the comparison variant


def terms(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOP}


def load_chunks() -> list[dict]:
    chunks = []
    for path in sorted((ROOT / "sample_data" / "policies").glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8-sig"))
        for d in chunk_markdown(body):
            chunks.append({"policy_id": meta["policy_id"], "title": meta["title"], "section": d.section, "text": d.text})
    return chunks


def rank(scores: np.ndarray) -> list[int]:
    return list(np.argsort(-scores, kind="stable"))


def main() -> None:
    settings = Settings(_env_file=None)
    embedder = get_embedder(settings)
    data = json.loads((ROOT / "evaluation" / "retrieval_dev.json").read_text())
    chunks = load_chunks()
    matrix = np.array(embedder.embed([embedding_text(c["title"], c["section"], c["text"]) for c in chunks]), dtype=np.float32)
    chunk_terms = [terms(c["section"] + " " + c["text"]) for c in chunks]

    def relevant_idx(item: dict) -> set[int]:
        wanted = {(p, s) for p, s in item["relevant"]}
        return {i for i, c in enumerate(chunks) if (c["policy_id"], c["section"]) in wanted}

    def scores_for(q: str, hybrid: bool) -> np.ndarray:
        sem = matrix @ np.array(embedder.embed([q])[0], dtype=np.float32)
        if not hybrid:
            return sem
        qt = terms(q)
        kw = np.array([len(qt & ct) / len(qt) if qt else 0.0 for ct in chunk_terms], dtype=np.float32)
        return sem + KEYWORD_WEIGHT * kw

    report: dict = {
        "date": date.today().isoformat(),
        "embedding_model": settings.embedding_model,
        "embedding_revision": settings.embedding_revision,
        "chunks_in_corpus": len(chunks),
        "policies": data["policies"],
        "answerable_questions": len(data["answerable"]),
        "unanswerable_questions": len(data["unanswerable"]),
        "note": "Synthetic DEV set; same author as the policies. Not real-world accuracy.",
    }

    for name, hybrid in (("semantic", False), ("semantic_plus_keyword", True)):
        hits1 = hits3 = 0
        rr = []
        for item in data["answerable"]:
            order = rank(scores_for(item["q"], hybrid))
            rel = relevant_idx(item)
            pos = next(i for i, idx in enumerate(order) if idx in rel) + 1
            hits1 += pos == 1
            hits3 += pos <= 3
            rr.append(1 / pos)
        n = len(data["answerable"])
        report[name] = {"hit@1": hits1 / n, "hit@3": hits3 / n, "mrr": statistics.mean(rr), "n": n}
        print(f"{name:24} hit@1 {hits1}/{n} = {hits1 / n:.2f} | hit@3 {hits3}/{n} = {hits3 / n:.2f} | MRR {statistics.mean(rr):.3f}")

    # --- score distributions and cutoff sweep for the PRODUCTION configuration:
    #     order = semantic + keyword (hybrid), gate = semantic cosine >= cutoff.
    ans_top = []  # (best semantic score, best semantic score among relevant chunks within the hybrid top 3)
    for item in data["answerable"]:
        sem = scores_for(item["q"], False)
        order = rank(scores_for(item["q"], True))
        rel = relevant_idx(item)
        in_top3 = [float(sem[i]) for i in order[:3] if i in rel]
        ans_top.append((float(sem.max()), max(in_top3) if in_top3 else None))
    una_top = [float(scores_for(item["q"], False).max()) for item in data["unanswerable"]]

    def summarize(values: list[float]) -> dict:
        return {"min": min(values), "median": statistics.median(values), "max": max(values)}

    ans_best = [t for t, _ in ans_top]
    print("\nbest score, answerable  :", {k: round(v, 3) for k, v in summarize(ans_best).items()})
    print("best score, unanswerable:", {k: round(v, 3) for k, v in summarize(una_top).items()})
    report["score_distribution"] = {"answerable_best": summarize(ans_best), "unanswerable_best": summarize(una_top)}

    sweep = []
    for t in [round(x * 0.01, 2) for x in range(10, 61)]:
        accepted_ok = sum(1 for _, rel_score in ans_top if rel_score is not None and rel_score >= t) / len(ans_top)
        rejected_ok = sum(1 for v in una_top if v < t) / len(una_top)
        sweep.append({"cutoff": t, "answerable_served": accepted_ok, "unanswerable_rejected": rejected_ok, "balanced": (accepted_ok + rejected_ok) / 2})
    best = max(s["balanced"] for s in sweep)
    plateau = [s["cutoff"] for s in sweep if s["balanced"] == best]
    chosen = round(statistics.median(plateau), 2)
    report["cutoff_sweep"] = sweep
    report["best_balanced_accuracy"] = best
    report["best_cutoff_plateau"] = [plateau[0], plateau[-1]]
    report["suggested_cutoff"] = chosen

    print("\ncutoff  answerable served   unanswerable rejected   balanced")
    for s in sweep:
        if s["cutoff"] * 100 % 5 == 0 or s["cutoff"] in plateau:
            mark = "  <-- in best plateau" if s["cutoff"] in plateau else ""
            print(f"{s['cutoff']:.2f}    {s['answerable_served']:.2f}                {s['unanswerable_rejected']:.2f}                    {s['balanced']:.2f}{mark}")
    print(f"\nbest balanced accuracy {best:.2f} for cutoffs {plateau[0]:.2f}..{plateau[-1]:.2f}; suggested cutoff (middle of plateau): {chosen}")
    wrongly_turned_away = [
        item["id"] for item, (_, rel_score) in zip(data["answerable"], ans_top) if rel_score is None or rel_score < chosen
    ]
    nearest_negative = max(una_top)
    print(f"answerable questions that would get 'policy does not address this' at {chosen}: {len(wrongly_turned_away)}/{len(ans_top)} {wrongly_turned_away}")
    print(f"highest unanswerable score {nearest_negative:.3f}; margin to cutoff {chosen - nearest_negative:+.3f}")
    report["at_suggested_cutoff"] = {
        "answerable_wrongly_turned_away": wrongly_turned_away,
        "highest_unanswerable_score": nearest_negative,
    }

    out = ROOT / "evaluation" / "results"
    out.mkdir(exist_ok=True)
    (out / "retrieval_dev.json").write_text(json.dumps(report, indent=2))
    print(f"wrote {out / 'retrieval_dev.json'}")


if __name__ == "__main__":
    main()
