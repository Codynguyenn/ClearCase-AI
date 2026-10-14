"""Would a "possibly related" tier help? Measured on the DEV set only (never the held-out set).

    backend/.venv/bin/python evaluation/weak_tier_dev.py

The idea: passages whose cosine is between a lower bound L and the strict cutoff would be shown
to the HUMAN reviewer only, labelled "weak match", and never given to the assistant as policy.

Adoption rule (fixed in advance, evaluation/README.md): adopt only if, on the dev set, the tier
surfaces a relevant passage for >= 30% of the answerable questions the strict cutoff wrongly refuses,
while showing weak passages for <= 50% of the unanswerable questions. The rule did not fix L, so
this script tries a small grid and the HIGHEST L meeting both conditions would be used. Choosing L
on dev is itself a tuning step; that is why the held-out set is measured afterwards.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.db import Mongo  # noqa: E402
from app.services.policy_service import PolicyService  # noqa: E402
from app.services.retrieval import MongoCosineRetriever  # noqa: E402

GRID = [0.25, 0.30, 0.35]
OWNER, DB = "eval-user", "clearcase_eval"


def main() -> None:
    data = json.loads((ROOT / "evaluation" / "retrieval_dev.json").read_text())
    settings = Settings(_env_file=None, mongodb_db=DB)
    cutoff = settings.retrieval_min_score
    mongo = Mongo(settings)
    mongo.client.drop_database(DB)
    mongo.ensure_indexes()
    try:
        PolicyService(mongo, settings).seed_samples(OWNER, Path(settings.sample_data_dir) / "policies")
        view = MongoCosineRetriever(mongo, settings.model_copy(update={"retrieval_min_score": -1.0, "retrieval_top_k_max": 100}))
        prod = MongoCosineRetriever(mongo, settings)

        rows = []
        for item in data["answerable"] + data["unanswerable"]:
            ranking = view.retrieve(OWNER, item["q"], top_k=100).passages
            status = prod.retrieve(OWNER, item["q"], top_k=3).status.value
            rows.append({"item": item, "ranking": ranking, "status": status})
    finally:
        mongo.client.drop_database(DB)
        mongo.close()

    answerable = [r for r in rows if "relevant" in r["item"]]
    unanswerable = [r for r in rows if "relevant" not in r["item"]]
    refused = [r for r in answerable if r["status"] == "no_relevant_policy"]
    print(f"strict cutoff {cutoff}: {len(refused)}/{len(answerable)} answerable questions are wrongly refused; {len(unanswerable)} unanswerable\n")

    results = []
    for low in GRID:
        def weak(r: dict) -> list:
            return [p for p in r["ranking"] if low <= p.score < cutoff][:3]

        recovered = sum(
            any((p.policy_id, p.section) in {(a, b) for a, b in r["item"]["relevant"]} for p in weak(r)) for r in refused
        )
        noise = sum(1 for r in unanswerable if weak(r))
        rec_rate, noise_rate = recovered / len(refused), noise / len(unanswerable)
        ok = rec_rate >= 0.30 and noise_rate <= 0.50
        results.append({"lower_bound": low, "recovered": recovered, "of_refused": len(refused), "recovered_rate": round(rec_rate, 3),
                        "unanswerable_with_weak_passages": noise, "of_unanswerable": len(unanswerable), "noise_rate": round(noise_rate, 3), "meets_rule": ok})
        print(f"L={low:.2f}: recovers {recovered}/{len(refused)} wrongly refused ({rec_rate:.0%}); weak passages shown for {noise}/{len(unanswerable)} unanswerable ({noise_rate:.0%})  -> {'MEETS RULE' if ok else 'does not meet rule'}")

    passing = [r for r in results if r["meets_rule"]]
    decision = max(passing, key=lambda r: r["lower_bound"])["lower_bound"] if passing else None
    print(f"\ndecision: {'adopt with L=' + str(decision) if decision else 'do NOT adopt (no L meets both conditions)'}")
    out = ROOT / "evaluation" / "results" / "weak_tier_dev.json"
    out.write_text(json.dumps({"set": "dev", "strict_cutoff": cutoff, "grid": results, "decision_lower_bound": decision, "note": "Synthetic dev set."}, indent=2))
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
