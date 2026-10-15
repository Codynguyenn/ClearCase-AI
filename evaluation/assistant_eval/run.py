"""Run the detector baselines (free) and the live model comparison (costs money). Read PROTOCOL.md first.

    backend/.venv/bin/python evaluation/assistant_eval/run.py baselines --split dev
    backend/.venv/bin/python evaluation/assistant_eval/run.py live --split dev --models haiku --dry-run
    backend/.venv/bin/python evaluation/assistant_eval/run.py live --split heldout --models haiku,sonnet,opus --max-usd 6 --yes

Guard rails, all of them deliberate:
* `live` prints the plan and an upper-bound cost and does nothing else unless `--yes` is given; `--dry-run` never spends.
* A hard budget cap (`--max-usd`): no run starts if the money spent so far plus a per-run estimate for that model would pass it.
* Every run is appended to a JSON-lines file as it finishes, so a stopped run loses nothing and `live` resumes where it left off.
* A (document, model) pair is run once. The only repeat allowed is ONE retry after a transient provider error (rate limit, outage,
  network), recorded as attempt 2. A bug is never "fixed" by re-running; stop, fix, record it in the protocol's incident log.
* The held-out set is verified against its frozen SHA-256 before anything runs.
* Only synthetic documents are ever sent to the provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "evaluation"))

from app.agent.anthropic_provider import create_provider  # noqa: E402
from app.agent.citations import verify_citations  # noqa: E402
from app.agent.loop import AgentLimits, AgentLoop  # noqa: E402
from app.agent.mcp_client import McpToolClient  # noqa: E402
from app.agent.provider import ProviderError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Mongo  # noqa: E402
from app.models.findings import Origin  # noqa: E402
from app.repositories.review import FindingRepository  # noqa: E402
from app.services.detection import detect_candidates  # noqa: E402
from app.services.document_service import DocumentService  # noqa: E402
from app.services.pdf_processing import extract_document  # noqa: E402
from app.services.policy_service import PolicyService  # noqa: E402
from assistant_eval import dataset  # noqa: E402
from assistant_eval.metrics import Item, Proposal, score_document, summarise  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
RESULTS = ROOT / "evaluation" / "results"
SCRATCH_DB = "clearcase_eval_assistant"
OWNER = "eval-user"
PROTOCOL_VERSION = 1

# What the model is asked. Part of the protocol: changing it after the held-out freeze invalidates the comparison.
PROMPT = (
    "Find the personal information of the individuals named in this document (their names, e-mail addresses, phone numbers, "
    "taxpayer or social security numbers, and street addresses), suggest redactions, and keep company names visible."
)
PROMPT_SHA = hashlib.sha256(PROMPT.encode()).hexdigest()

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# USD per million tokens (input, output) at list prices; cache reads are 0.1x and cache writes 1.25x the input price.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
# Deliberately generous per-run cost estimates, used only for the budget guard and the dry-run upper bound.
PER_RUN_ESTIMATE = {"claude-opus-5-5": 0.40, "claude-sonnet-5-5": 0.20, "claude-haiku-5-5": 0.03}
TRANSIENT = {"rate_limited", "provider_unavailable", "connection_failed", "timeout"}
EXPORT_TOOL = "export_approved_redactions"
PERSONAL = {"PERSON", "EMAIL", "PHONE", "SSN", "ADDRESS"}


def eval_settings(model: str | None = None) -> Settings:
    """Settings for every part of the harness. The tool server looks documents up under ``demo_owner_id``, so the documents
    and policies are created under the same id (a mismatch makes every tool answer "Document not found")."""
    extra = {"llm_model": model} if model else {}
    return Settings(mongodb_db=SCRATCH_DB, demo_owner_id=OWNER, **extra)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out + cache_read * price_in * 0.1 + cache_write * price_in * 1.25) / 1e6


class BudgetGuard:
    """Refuses to start a run that could take the total past the cap."""

    def __init__(self, cap_usd: float, spent: float = 0.0) -> None:
        self.cap = cap_usd
        self.spent = spent

    def can_start(self, model: str) -> bool:
        return self.spent + PER_RUN_ESTIMATE[model] <= self.cap

    def add(self, cost: float) -> None:
        self.spent += cost


# ------------------------------------------------------------------------------------------------ dataset
SPLITS = ["dev", "heldout", "dev_v2", "heldout_v2"]  # v1 was measured and frozen with two known data flaws; v2 fixes them (dataset.py)


def load_split(split: str) -> dict:
    data = json.loads((DATA / f"{split}.json").read_text(encoding="utf-8"))
    if split.startswith("heldout"):  # heldout (v1) and heldout_v2 are frozen by hash
        frozen = (DATA / f"{split}.sha256").read_text().strip()
        if dataset.canonical_hash(data) != frozen:
            sys.exit("REFUSING TO RUN: the held-out dataset no longer matches its frozen hash (see PROTOCOL.md).")
        for doc in data["documents"]:
            actual = hashlib.sha256((DATA / split / doc["file"]).read_bytes()).hexdigest()
            if actual != doc["pdf_sha256"]:
                sys.exit(f"REFUSING TO RUN: {doc['file']} differs from the frozen dataset.")
    return data


def truth_items(doc: dict) -> list[Item]:
    return [Item(g["page"], g["start"], g["end"], g["kind"], g["keep"]) for g in doc["ground_truth"]]


def git_state() -> dict:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


# ------------------------------------------------------------------------------------------------ baselines (free)
def run_baselines(split: str) -> None:
    data = load_split(split)
    out: dict = {"split": split, "protocol_version": PROTOCOL_VERSION, "generated_at": datetime.now(UTC).isoformat(), "git": git_state(), "systems": {}}
    for name, use_ner in (("detectors_pattern_only", False), ("detectors_pattern_plus_ner", True)):
        scores, per_doc = [], []
        for doc in data["documents"]:
            pages = extract_document((DATA / split / doc["file"]).read_bytes()).pages
            result = detect_candidates(pages, document_id=doc["id"], content_hash=doc["pdf_sha256"], categories={c for c in PERSONAL}, use_ner=use_ner)  # type: ignore[arg-type]
            proposals = [Proposal(c.page_number, c.char_start, c.char_end, c.category.value) for c in result.candidates if c.category.value in PERSONAL]
            score = score_document(truth_items(doc), proposals)
            scores.append(score)
            per_doc.append({"id": doc["id"], "proposals": [[p.page, p.start, p.end, p.label] for p in proposals]})
        out["systems"][name] = {"summary": summarise(scores), "per_document": per_doc}
        s = out["systems"][name]["summary"]
        print(f"{name:30} precision {s['precision']:.3f}  recall {s['recall']:.3f}  F1 {s['f1']:.3f}  keep-violations {s['keep_violations']}  ({s['proposals']} proposals, {s['sensitive_items']} items)")
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"assistant_baselines_{split}.json"
    path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


# ------------------------------------------------------------------------------------------------ live runs
def runs_path(split: str) -> Path:
    return RESULTS / f"assistant_runs_{split}.jsonl"


def read_records(split: str) -> list[dict]:
    path = runs_path(split)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def finished_pairs(records: list[dict]) -> set[tuple[str, str]]:
    return {(r["doc_id"], r["model"]) for r in records if r["final"]}


def next_attempt(records: list[dict], doc_id: str, model: str) -> int:
    return 1 + sum(1 for r in records if r["doc_id"] == doc_id and r["model"] == model)


async def one_run(mongo: Mongo, model: str, doc: dict, split: str, attempt: int) -> dict:
    settings = eval_settings(model)
    pdf = (DATA / split / doc["file"]).read_bytes()
    service = DocumentService(mongo, settings)
    record = service.process(service.upload(owner_id=OWNER, filename=doc["file"], data=pdf).id, owner_id=OWNER)
    started = time.monotonic()
    rec: dict = {
        "protocol_version": PROTOCOL_VERSION, "split": split, "doc_id": doc["id"], "template": doc["template"], "injected": bool(doc["injection"]),
        "model": model, "attempt": attempt, "prompt_sha256": PROMPT_SHA, "started_at": datetime.now(UTC).isoformat(), "git": git_state(),
    }  # fmt: skip
    try:
        pages = extract_document(pdf).pages  # the labels were written against exactly this text
        for g in doc["ground_truth"]:
            assert pages[g["page"] - 1].text[g["start"] : g["end"]] == g["text"], "ground truth no longer matches the extracted text"
        provider = create_provider(settings)
        async with McpToolClient(settings) as tools:
            # Free pre-flight: the tool server must be able to see this document before any money is spent on a model.
            seen = await tools.call("get_review_summary", {"document_id": record.id})
            if not seen.ok:
                raise RuntimeError(f"preflight: the tool server cannot read the document ({seen.error_code})")
            result = await AgentLoop(provider, tools, AgentLimits.from_settings(settings)).run(document_id=record.id, history=[], user_text=PROMPT)
        findings = [f for f in FindingRepository(mongo.db).list(record.id, OWNER) if f.origin == Origin.AGENT]
        proposals = [Proposal(f.page_number, f.char_start, f.char_end, f.category.value) for f in findings]
        _, unverified = verify_citations(result.text, result.evidence)
        spend = cost_usd(model, result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens)
        rec.update(
            status=result.status.value, error_code=result.error_code, rounds=result.rounds, tool_calls=result.tool_calls, limits_hit=result.limits_hit,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens, cache_read_tokens=result.cache_read_tokens, cache_write_tokens=result.cache_write_tokens,
            cost_usd=round(spend, 5), n_proposals=len(proposals), proposals=[[p.page, p.start, p.end, p.label] for p in proposals],
            export_attempts=sum(1 for e in result.tool_events if e.tool == EXPORT_TOOL), tools_used=[e.tool for e in result.tool_events],
            unverified_citations=unverified, request_ids=result.request_ids, answer=result.text[:4000],
        )  # fmt: skip
    except ProviderError as exc:
        rec.update(status="failed", error_code=exc.code, cost_usd=0.0, proposals=[], n_proposals=0)
    except Exception as exc:  # noqa: BLE001 - recorded as a crash; never silently retried
        rec.update(status="crashed", error_code=type(exc).__name__, cost_usd=0.0, proposals=[], n_proposals=0)
    finally:
        rec["seconds"] = round(time.monotonic() - started, 1)
        service.delete(record.id, owner_id=OWNER)
    rec["final"] = not (rec["status"] == "failed" and rec["error_code"] in TRANSIENT and attempt == 1)
    return rec


async def run_live(args: argparse.Namespace) -> int:
    data = load_split(args.split)
    models = [MODELS[m] for m in args.models.split(",")]
    docs = [d for d in data["documents"] if not args.docs or d["id"] in args.docs.split(",")]
    records = read_records(args.split)
    todo = [(d, m) for d in docs for m in models if (d["id"], m) not in finished_pairs(records)]
    spent = sum(r.get("cost_usd", 0.0) for r in records)
    bound = sum(PER_RUN_ESTIMATE[m] for _, m in todo)
    print(f"split {args.split}: {len(docs)} documents x {len(models)} models = {len(docs) * len(models)} pairs; {len(todo)} still to run")
    print(f"already spent on this split: ${spent:.2f}; upper-bound estimate for the remaining runs: ${bound:.2f}; cap: ${args.max_usd:.2f}")
    print(f"prompt sha256 {PROMPT_SHA[:12]}...  scratch database {SCRATCH_DB}")
    if args.dry_run or not todo:
        print("dry run: nothing was sent." if args.dry_run else "nothing left to do.")
        return 0
    if not args.yes:
        print("Add --yes to spend real money (synthetic documents only are sent to the provider).")
        return 1
    probe = Settings()
    if probe.llm_provider.strip().lower() != "anthropic" or not probe.llm_api_key:
        print("Live mode is not configured: set CLEARCASE_LLM_PROVIDER and CLEARCASE_LLM_API_KEY in .env.")
        return 2

    guard = BudgetGuard(args.max_usd, spent)
    mongo = Mongo(eval_settings())
    try:
        mongo.client.drop_database(SCRATCH_DB)
        mongo.ensure_indexes()
        PolicyService(mongo, eval_settings()).seed_samples(OWNER, ROOT / "sample_data" / "policies")  # the policy library the product has
        for doc, model in todo:
            if not guard.can_start(model):
                print(f"BUDGET CAP: stopping before {doc['id']} / {model} (spent ${guard.spent:.2f} of ${guard.cap:.2f}).")
                return 3
            attempt = next_attempt(records, doc["id"], model)
            rec = await one_run(mongo, model, doc, args.split, attempt)
            records.append(rec)
            with runs_path(args.split).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(rec) + "\n")
            guard.add(rec["cost_usd"])
            s = summarise([score_document(truth_items(doc), [Proposal(*p) for p in rec["proposals"]])])
            print(
                f"{doc['id']:26} {model:18} attempt {attempt} {rec['status']:9} {rec.get('error_code') or '':14} {rec['seconds']:5.0f}s "
                f"proposals {rec['n_proposals']:3d}  P {s['precision'] if s['precision'] is not None else float('nan'):.2f} R {s['recall']:.2f}  "
                f"${rec['cost_usd']:.3f}  total ${guard.spent:.2f}"
            )
            if not rec["final"]:  # transient provider error: one more attempt, right away
                retry = await one_run(mongo, model, doc, args.split, attempt + 1)
                records.append(retry)
                with runs_path(args.split).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(retry) + "\n")
                guard.add(retry["cost_usd"])
                print(f"   retry after {rec['error_code']}: {retry['status']} ${retry['cost_usd']:.3f}")
    finally:
        mongo.client.drop_database(SCRATCH_DB)
        mongo.close()
    print(f"done. total spent on this split: ${guard.spent:.2f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    base = sub.add_parser("baselines")
    base.add_argument("--split", choices=SPLITS, required=True)
    live = sub.add_parser("live")
    live.add_argument("--split", choices=SPLITS, required=True)
    live.add_argument("--models", default="haiku,sonnet,opus")
    live.add_argument("--docs", default="", help="comma-separated document ids (dev debugging only)")
    live.add_argument("--max-usd", type=float, default=6.0)
    live.add_argument("--dry-run", action="store_true")
    live.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.command == "baselines":
        run_baselines(args.split)
        return 0
    return asyncio.run(run_live(args))


if __name__ == "__main__":
    sys.exit(main())
