"""Compare small embedding models on the DEV question set only (never the held-out set).

    backend/.venv/bin/python evaluation/compare_models.py

For each model it uses the same pipeline the app uses (passage text = title + section + text;
order = cosine + 0.15 * keyword overlap; show a passage only if its plain cosine reaches a
cutoff) and reports hit@1, hit@3, MRR, and the best dev cutoff with its balanced accuracy and
margin. Models are downloaded into .local/model_compare (git-ignored), NOT into the app's
model folder. Each model's revision is pinned below.

The adoption rule (fixed in advance, see evaluation/README.md): a candidate is adopted only if
its best-cutoff balanced accuracy AND MRR are each >= 0.05 higher than the current model's, and
its margin is not smaller than the current model's. All numbers are synthetic dev-set numbers.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402

from app.services.policy_chunking import chunk_markdown, parse_front_matter  # noqa: E402
from app.services.policy_service import embedding_text  # noqa: E402
from app.services.retrieval import _terms  # noqa: E402  (the app's own keyword helper)

KEYWORD_WEIGHT = 0.15
CACHE = ROOT / ".local" / "model_compare"

# name -> (repo, pinned revision, query prefix, licence per its model card)
MODELS = {
    "all-MiniLM-L6-v2 (current)": ("sentence-transformers/all-MiniLM-L6-v2", "1110a243fdf4706b3f48f1d95db1a4f5529b4d41", "", "Apache-2.0"),
    "all-MiniLM-L12-v2": ("sentence-transformers/all-MiniLM-L12-v2", "a50ef00143b4d5391434df20ae11632588ac25be", "", "Apache-2.0"),
    "bge-small-en-v1.5": ("BAAI/bge-small-en-v1.5", "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a", "Represent this sentence for searching relevant passages: ", "MIT"),
    "gte-small": ("thenlper/gte-small", "17e1f347d17fe144873b1201da91788898c639cd", "", "MIT"),
}


def load_chunks() -> list[dict]:
    out = []
    for path in sorted((ROOT / "sample_data" / "policies").glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8-sig"))
        for d in chunk_markdown(body):
            out.append({"policy_id": meta["policy_id"], "title": meta["title"], "section": d.section, "text": d.text})
    return out


def evaluate(name: str, repo: str, revision: str, prefix: str, chunks: list[dict], data: dict) -> dict:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(repo, revision=revision, cache_folder=str(CACHE), device="cpu", trust_remote_code=False)
    passages = np.array(model.encode([embedding_text(c["title"], c["section"], c["text"]) for c in chunks], normalize_embeddings=True))
    chunk_terms = [_terms(c["section"] + " " + c["text"]) for c in chunks]

    def scores(q: str) -> tuple[np.ndarray, np.ndarray]:
        cos = passages @ np.array(model.encode([prefix + q], normalize_embeddings=True)[0])
        qt = _terms(q)
        kw = np.array([len(qt & ct) / len(qt) if qt else 0.0 for ct in chunk_terms])
        return cos, cos + KEYWORD_WEIGHT * kw

    def relevant(item: dict) -> set[int]:
        want = {(p, s) for p, s in item["relevant"]}
        return {i for i, c in enumerate(chunks) if (c["policy_id"], c["section"]) in want}

    ranks, best_rel_in_top3, ans_best = [], [], []
    for item in data["answerable"]:
        cos, order_score = scores(item["q"])
        order = list(np.argsort(-order_score, kind="stable"))
        rel = relevant(item)
        ranks.append(next(i for i, idx in enumerate(order, 1) if idx in rel))
        top3 = [float(cos[i]) for i in order[:3] if i in rel]
        best_rel_in_top3.append(max(top3) if top3 else None)
        ans_best.append(float(cos.max()))
    una_best = [float(scores(item["q"])[0].max()) for item in data["unanswerable"]]

    n = len(ranks)
    sweep = []
    for t in [round(x * 0.01, 2) for x in range(0, 96)]:
        served = sum(1 for s in best_rel_in_top3 if s is not None and s >= t) / n
        refused = sum(1 for v in una_best if v < t) / len(una_best)
        sweep.append((t, served, refused, (served + refused) / 2))
    best = max(s[3] for s in sweep)
    plateau = [s[0] for s in sweep if s[3] == best]
    cutoff = round(statistics.median(plateau), 2)
    chosen = next(s for s in sweep if s[0] == cutoff)
    return {
        "model": name,
        "repo": repo,
        "revision": revision,
        "hit@1": sum(r == 1 for r in ranks) / n,
        "hit@3": sum(r <= 3 for r in ranks) / n,
        "mrr": statistics.mean(1 / r for r in ranks),
        "best_cutoff": cutoff,
        "plateau": [plateau[0], plateau[-1]],
        "answerable_served": chosen[1],
        "unanswerable_refused": chosen[2],
        "balanced_accuracy": chosen[3],
        "margin": round(cutoff - max(una_best), 4),
        "highest_unanswerable_cosine": round(max(una_best), 4),
        "answerable_best_cosine_median": round(statistics.median(ans_best), 3),
    }


def main() -> None:
    data = json.loads((ROOT / "evaluation" / "retrieval_dev.json").read_text())
    assert "retrieval_dev" in str(ROOT / "evaluation" / "retrieval_dev.json")  # dev set only, by construction
    chunks = load_chunks()
    CACHE.mkdir(parents=True, exist_ok=True)
    results = []
    for name, (repo, revision, prefix, licence) in MODELS.items():
        r = evaluate(name, repo, revision, prefix, chunks, data)
        r["licence"] = licence
        results.append(r)
        print(f"{name:28} hit@1 {r['hit@1']:.2f} hit@3 {r['hit@3']:.2f} MRR {r['mrr']:.3f} | best cutoff {r['best_cutoff']:.2f} "
              f"(plateau {r['plateau'][0]:.2f}-{r['plateau'][1]:.2f}) balanced {r['balanced_accuracy']:.2f} margin {r['margin']:+.3f}")

    base = results[0]
    print("\nAdoption rule: balanced +0.05 AND MRR +0.05 AND margin >= current margin")
    for r in results[1:]:
        d_bal, d_mrr = r["balanced_accuracy"] - base["balanced_accuracy"], r["mrr"] - base["mrr"]
        ok = d_bal >= 0.05 and d_mrr >= 0.05 and r["margin"] >= base["margin"]
        r["meets_adoption_rule"] = ok
        print(f"  {r['model']:24} d_balanced {d_bal:+.3f}  d_MRR {d_mrr:+.3f}  margin {r['margin']:+.3f} vs {base['margin']:+.3f}  -> {'MEETS RULE' if ok else 'does not meet rule'}")

    out = ROOT / "evaluation" / "results" / "model_comparison_dev.json"
    out.write_text(json.dumps({"set": "dev", "note": "Synthetic dev set; not real-world accuracy.", "keyword_weight": KEYWORD_WEIGHT, "results": results}, indent=2))
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
