"""Measure processing latency and export-verification outcomes on the synthetic documents. Read PROTOCOL.md first.

    backend/.venv/bin/python evaluation/pipeline_eval/run.py            # needs MongoDB; free; about a minute
    backend/.venv/bin/python evaluation/pipeline_eval/run.py --passes 1 # quicker, for debugging

Writes evaluation/results/pipeline_eval.json (every document, every pass, every time) and evaluation/pipeline_eval/REPORT.md.
Everything runs in-process against a scratch database; no language model and no network are used.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "evaluation"))

HERE = Path(__file__).resolve().parent
DATA = ROOT / "evaluation" / "assistant_eval" / "data"
RESULTS = ROOT / "evaluation" / "results" / "pipeline_eval.json"  # v1 data (the first, flawed set); v2 writes pipeline_eval_v2.json
REPORT = HERE / "REPORT.md"  # likewise REPORT_v2.md
SCRATCH_DB = "clearcase_eval_pipeline"
PERSONAL = ["PERSON", "EMAIL", "PHONE", "SSN", "ADDRESS"]
ATTRIBUTION_MIN = 0.5  # the assistant evaluation's rule: at least half of the proposal lies inside the ground-truth item


# ------------------------------------------------------------------------------------------------ pure helpers (tested)
def attribute(page: int, start: int, end: int, truth: Sequence[dict]) -> dict | None:
    """The ground-truth item a proposal is attributed to (largest overlap, at least half of the proposal inside it), else None."""
    best, best_overlap = None, 0
    for item in truth:
        if item["page"] == page:
            overlap = max(0, min(end, item["end"]) - max(start, item["start"]))
            if overlap > best_overlap:
                best, best_overlap = item, overlap
    return best if best is not None and best_overlap >= ATTRIBUTION_MIN * max(end - start, 1) else None


def reviewer_decision(page: int, start: int, end: int, truth: Sequence[dict]) -> str:
    """The simulated careful reviewer: approve what points at personal information, reject everything else."""
    item = attribute(page, start, end, truth)
    return "approve" if item is not None and not item["keep"] else "reject"


def approved_still_readable(output_text: str, approved_texts: Sequence[str], keep_texts: Sequence[str]) -> int:
    """Occurrences of approved text left in the output, ignoring ones that are part of a kept company name."""
    text = output_text
    for keep in sorted(set(keep_texts), key=len, reverse=True):
        text = text.replace(keep, "\0")
    return sum(text.count(a) for a in set(approved_texts) if a)


def keep_preserved(original_text: str, output_text: str, keep_texts: Sequence[str]) -> tuple[int, int]:
    """(occurrences of kept company names in the output, capped at the original count; occurrences in the original)."""
    kept = total = 0
    for k in set(keep_texts):
        original = original_text.count(k)
        total += original
        kept += min(output_text.count(k), original)
    return kept, total


def texts_with_unapproved_occurrences(original_text: str, approved_findings_texts: Sequence[str]) -> int:
    """How many approved texts occur more often in the original than they were approved: some occurrence was never proposed."""
    approved = Counter(approved_findings_texts)
    return sum(1 for t, n in approved.items() if original_text.count(t) > n)


def percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def stats(values: Sequence[float]) -> dict:
    return {"n": len(values), "median": statistics.median(values), "p90": percentile(values, 0.9), "max": max(values), "min": min(values)}


# ------------------------------------------------------------------------------------------------ the measurement
def load_documents(variant: str = "v1") -> list[tuple[str, dict, bytes]]:
    docs = []
    for split in (("dev", "heldout") if variant == "v1" else ("dev_v2", "heldout_v2")):
        for doc in json.loads((DATA / f"{split}.json").read_text(encoding="utf-8"))["documents"]:
            docs.append((split, doc, (DATA / split / doc["file"]).read_bytes()))
    return docs


def run_pass(client, docs, timed: bool = True) -> list[dict]:
    from app.services.detection import detect_candidates
    from app.services.pdf_processing import extract_document

    rows = []
    for split, doc, pdf in docs:
        truth = doc["ground_truth"]
        row: dict = {"split": split, "doc_id": doc["id"], "template": doc["template"], "pages": doc["pages"]}

        t = time.perf_counter()
        extracted = extract_document(pdf)
        row["ms_extract_library"] = (time.perf_counter() - t) * 1000
        original_text = "".join(p.text for p in extracted.pages)

        t = time.perf_counter()
        r = client.post("/api/documents", files={"file": (doc["file"], pdf, "application/pdf")})
        assert r.status_code in (200, 201, 202), r.text
        doc_id = r.json()["id"]
        while client.get(f"/api/documents/{doc_id}").json()["status"] not in ("ready", "unsupported", "error"):
            time.sleep(0.01)
        row["ms_upload_to_ready"] = (time.perf_counter() - t) * 1000
        row["status_after_upload"] = client.get(f"/api/documents/{doc_id}").json()["status"]

        # detector-only timings through the library (pattern-only, then with NER), to show what NER costs
        pages = extracted.pages
        for name, use_ner in (("ms_detect_pattern_only", False), ("ms_detect_with_ner", True)):
            t = time.perf_counter()
            detect_candidates(pages, document_id=doc_id, content_hash="x", categories=set(PERSONAL), use_ner=use_ner)  # type: ignore[arg-type]
            row[name] = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        created = client.post(f"/api/documents/{doc_id}/findings/propose-detected", json={"categories": PERSONAL}).json()["created"]
        row["ms_propose_detected"] = (time.perf_counter() - t) * 1000

        decisions = {f["id"]: reviewer_decision(f["page_number"], f["char_start"], f["char_end"], truth) for f in created}
        approve = [i for i, d in decisions.items() if d == "approve"]
        reject = [i for i, d in decisions.items() if d == "reject"]
        for ids, action in ((approve, "approve"), (reject, "reject")):
            if ids:
                client.post(f"/api/documents/{doc_id}/findings/bulk-decision", json={"action": action, "finding_ids": ids})
        approved_texts = [f["text"] for f in created if decisions[f["id"]] == "approve"]
        row.update(proposed=len(created), approved=len(approve), rejected=len(reject))

        t = time.perf_counter()
        token = client.post(f"/api/documents/{doc_id}/export-authorizations").json()["token"]
        export = client.post(f"/api/documents/{doc_id}/exports", json={"authorization": token}).json()
        row["ms_export"] = (time.perf_counter() - t) * 1000

        needed_ack = bool(export.get("requires_acknowledgement"))
        if needed_ack:
            export = client.post(f"/api/documents/{doc_id}/exports/{export['id']}/acknowledge-warnings").json()
        failed = [c["name"] for c in export.get("checks", []) if c["required"] and not c["passed"]]
        advisory = [c["name"] for c in export.get("checks", []) if not c["required"] and not c["passed"]]
        row.update(
            outcome=("verified_needs_acknowledgement" if needed_ack else "verified") if export["status"] == "verified" else export["status"],
            needed_acknowledgement=needed_ack, unresolved_copies=export["retained"]["unresolved"],
            required_checks_failed=failed, advisory_checks_failed=advisory, checks=len(export.get("checks", [])),
            texts_with_unapproved_occurrences=texts_with_unapproved_occurrences(original_text, approved_texts),
        )  # fmt: skip

        keep_texts = [g["text"] for g in truth if g["keep"]]
        if export.get("downloadable"):
            out = client.get(f"/api/documents/{doc_id}/exports/{export['id']}/file").content
            output_text = "".join(p.text for p in extract_document(out).pages)
            row["approved_still_readable"] = approved_still_readable(output_text, approved_texts, keep_texts)
            kept, total = keep_preserved(original_text, output_text, keep_texts)
            row["keep_preserved"], row["keep_total"] = kept, total
        else:
            row["approved_still_readable"] = row["keep_preserved"] = row["keep_total"] = None

        sensitive = [g for g in truth if not g["keep"]]
        approved_spans = [(f["page_number"], f["char_start"], f["char_end"]) for f in created if decisions[f["id"]] == "approve"]
        covered = 0
        items_by_category, covered_by_category = Counter(), Counter()
        for g in sensitive:
            cover = set()
            for page, s, e in approved_spans:
                if page == g["page"]:
                    cover |= set(range(max(s, g["start"]), min(e, g["end"])))
            hit = len(cover) >= 0.8 * (g["end"] - g["start"])
            covered += hit
            items_by_category[g["kind"]] += 1
            covered_by_category[g["kind"]] += hit
        row.update(sensitive_items=len(sensitive), sensitive_covered=covered, items_by_category=dict(items_by_category), covered_by_category=dict(covered_by_category))
        client.delete(f"/api/documents/{doc_id}")
        rows.append(row)
    return rows


def aggregate(passes: list[list[dict]]) -> dict:
    docs = [r["doc_id"] for r in passes[0]]
    first = {r["doc_id"]: r for r in passes[0]}
    stages = ["ms_extract_library", "ms_upload_to_ready", "ms_detect_pattern_only", "ms_detect_with_ner", "ms_propose_detected", "ms_export"]
    per_doc = {s: [statistics.median(next(r for r in p if r["doc_id"] == d)[s] for p in passes) for d in docs] for s in stages}
    pages = [first[d]["pages"] for d in docs]
    lat = {s: stats(v) for s, v in per_doc.items()}
    lat["per_page_ms_upload_to_ready"] = stats([v / p for v, p in zip(per_doc["ms_upload_to_ready"], pages, strict=True)])
    lat["per_page_ms_export"] = stats([v / p for v, p in zip(per_doc["ms_export"], pages, strict=True)])
    rows = list(first.values())
    outcomes = Counter(r["outcome"] for r in rows)
    readable = [r["approved_still_readable"] for r in rows if r["approved_still_readable"] is not None]
    approved_total = sum(r["approved"] for r in rows)
    return {
        "documents": len(rows), "pages": sum(pages), "passes": len(passes), "latency_ms": lat,
        "outcomes": dict(outcomes),
        "outcomes_stable_across_passes": all([r["outcome"] for r in p] == [r["outcome"] for r in passes[0]] for p in passes),
        "approved_findings": approved_total, "rejected_findings": sum(r["rejected"] for r in rows), "proposed_findings": sum(r["proposed"] for r in rows),
        "approved_text_still_readable": {"occurrences": sum(readable), "documents_checked": len(readable)},
        "keep_preserved": {"kept": sum(r["keep_preserved"] for r in rows if r["keep_total"] is not None), "of": sum(r["keep_total"] for r in rows if r["keep_total"] is not None)},
        "sensitive_items": sum(r["sensitive_items"] for r in rows), "sensitive_covered": sum(r["sensitive_covered"] for r in rows),
        "coverage_by_category": {c: {"items": sum(r["items_by_category"].get(c, 0) for r in rows), "covered": sum(r["covered_by_category"].get(c, 0) for r in rows)} for c in PERSONAL},
        "required_checks_failed": sum(len(r["required_checks_failed"]) for r in rows),
        "needed_acknowledgement": [r["doc_id"] for r in rows if r["needed_acknowledgement"]],
        "ack_cause_is_partial_proposal": all(r["texts_with_unapproved_occurrences"] > 0 for r in rows if r["needed_acknowledgement"]),
    }  # fmt: skip


def predictions(a: dict) -> list[dict]:
    lat = a["latency_ms"]
    clean = a["outcomes"].get("verified", 0)
    bad = a["outcomes"].get("verification_failed", 0) + a["outcomes"].get("blocked", 0)
    return [
        {"prediction": "1. At least 15 of 18 exports are verified without needing acknowledgement; none failed or blocked.", "held": clean >= 15 and bad == 0, "detail": f"{a['outcomes']}"},
        {"prediction": "2. Approved text still readable: 0 occurrences; kept company names preserved: 100%.", "held": a["approved_text_still_readable"]["occurrences"] == 0 and a["keep_preserved"]["kept"] == a["keep_preserved"]["of"], "detail": f"readable {a['approved_text_still_readable']['occurrences']}; kept {a['keep_preserved']['kept']}/{a['keep_preserved']['of']}"},
        {"prediction": "3. Median upload-to-ready under 1 s and median export under 2 s.", "held": lat["ms_upload_to_ready"]["median"] < 1000 and lat["ms_export"]["median"] < 2000, "detail": f"upload-to-ready {lat['ms_upload_to_ready']['median']:.0f} ms; export {lat['ms_export']['median']:.0f} ms"},
        {"prediction": "4. Whenever an export needs acknowledgement, a detector proposed some occurrences of a text but not all.", "held": a["ack_cause_is_partial_proposal"], "detail": f"{len(a['needed_acknowledgement'])} document(s) needed acknowledgement"},
    ]  # fmt: skip


def render(a: dict, preds: list[dict], env: dict, rows: list[dict], variant: str = "v1") -> str:
    lat = a["latency_ms"]
    f = lambda s: f"{lat[s]['median']:.0f} ({lat[s]['p90']:.0f} / {lat[s]['max']:.0f})"  # noqa: E731
    o = a["outcomes"]
    out = [
        f"# Pipeline measurement: latency and export-verification outcomes (synthetic, {variant} data)", "",
        "Generated by `run.py` from the saved raw results. Read `PROTOCOL.md` first: the rules, outcomes and predictions were written before the run. "
        f"{a['documents']} synthetic documents ({a['pages']} pages), {a['passes']} passes after a warm-up, in-process against a real MongoDB scratch database. "
        f"Machine: {env['platform']}, Python {env['python']}, PyMuPDF {env['pymupdf']}, spaCy {env['spacy']}. No model, no network, no spending.", "",
        "## Export-verification outcomes (denominator: 18 documents)", "",
        "| Outcome | Documents |", "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in sorted(o.items())],
        "", f"Outcomes were the same in every pass: {a['outcomes_stable_across_passes']}. Required checks failed: {a['required_checks_failed']}. "
        f"Documents that needed acknowledgement: {', '.join(a['needed_acknowledgement']) or 'none'}.", "",
        "## Independent re-check of the downloaded files", "",
        f"- Approved findings: {a['approved_findings']} (of {a['proposed_findings']} proposed; {a['rejected_findings']} rejected as not personal information).",
        f"- **Approved text still readable in the output: {a['approved_text_still_readable']['occurrences']} occurrences** (checked in {a['approved_text_still_readable']['documents_checked']} files; occurrences inside kept company names ignored).",
        f"- **Kept company names preserved: {a['keep_preserved']['kept']} of {a['keep_preserved']['of']}** occurrences.",
        f"- Personal items covered by an approval at the end of the pipeline: **{a['sensitive_covered']} of {a['sensitive_items']}**. The rest were never proposed, so they remain readable "
        "and **the verifier cannot know about them**: this is the detectors' recall, not a verification failure.",
        "- By category (covered / items): " + "; ".join(f"{c} {v['covered']}/{v['items']}" for c, v in a["coverage_by_category"].items()) + ". "
        + (
            f"**PHONE is 0 because the synthetic phone numbers of this first (v1) data set are malformed** (9 digits after the country code; see the assistant evaluation's report): no validating detector can match them. "
            f"Without the {a['coverage_by_category']['PHONE']['items']} phone items, coverage is {a['sensitive_covered'] - a['coverage_by_category']['PHONE']['covered']} of {a['sensitive_items'] - a['coverage_by_category']['PHONE']['items']}."
            if variant == "v1"
            else "This is the corrected (v2) data set: valid phone numbers, company addresses not labelled as personal."
        ), "",
        "## Latency (milliseconds; median over passes per document, then median (90th percentile / max) over documents)", "",
        "| Stage | Median (p90 / max) |", "| --- | --- |",
        f"| Text extraction, library call only | {f('ms_extract_library')} |",
        f"| Upload to \"ready\" (validate, store, extract, store pages) | {f('ms_upload_to_ready')} |",
        f"| Detection, pattern detectors only (library) | {f('ms_detect_pattern_only')} |",
        f"| Detection, pattern plus NER (library) | {f('ms_detect_with_ner')} |",
        f"| Propose detected findings (API: detection plus storing) | {f('ms_propose_detected')} |",
        f"| Export (authorize, redact, verify, store) | {f('ms_export')} |",
        f"| Upload to ready, per page | {lat['per_page_ms_upload_to_ready']['median']:.0f} ({lat['per_page_ms_upload_to_ready']['p90']:.0f} / {lat['per_page_ms_upload_to_ready']['max']:.0f}) |",
        f"| Export, per page | {lat['per_page_ms_export']['median']:.0f} ({lat['per_page_ms_export']['p90']:.0f} / {lat['per_page_ms_export']['max']:.0f}) |", "",
        "## Predictions made before the run (PROTOCOL.md section 4)", "", "| Prediction | Held? | Detail |", "| --- | --- | --- |",
        *[f"| {p['prediction']} | {'yes' if p['held'] else '**no**'} | {p['detail']} |" for p in preds], "",
        "## Per document", "", "| Document | Pages | Proposed / approved / rejected | Outcome | Approved text readable | Kept names preserved | Personal items covered | Upload to ready (ms) | Export (ms) |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        *[f"| {r['doc_id']} | {r['pages']} | {r['proposed']} / {r['approved']} / {r['rejected']} | {r['outcome']} | {r['approved_still_readable']} | {r['keep_preserved']}/{r['keep_total']} | {r['sensitive_covered']}/{r['sensitive_items']} | {r['ms_upload_to_ready']:.0f} | {r['ms_export']:.0f} |" for r in rows],
        "", "## Limits", "",
        "Synthetic one- and two-page documents from one author; a simulated reviewer who never errs; in-process timing on one machine, so network and "
        "large files are not represented; the verifier checks what was approved and cannot find sensitive text nobody proposed; scanned, mixed and encrypted "
        "PDFs are covered by the test suite, not measured here.",
    ]  # fmt: skip
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--variant", choices=["v1", "v2"], default="v1", help="v1: the first data set (two known flaws); v2: the corrected one")
    args = parser.parse_args()

    import pymupdf
    import spacy
    from fastapi.testclient import TestClient
    from pymongo import MongoClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(_env_file=None, mongodb_db=SCRATCH_DB, reindex_on_startup=False)
    env = {"platform": platform.platform(), "python": platform.python_version(), "pymupdf": pymupdf.__version__, "spacy": spacy.__version__, "cpus": os.cpu_count()}
    docs = load_documents(args.variant)
    mongo = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=3000)
    passes: list[list[dict]] = []
    try:
        for index in range(args.passes + 1):  # pass 0 is the warm-up (it loads the NER model) and is kept apart
            mongo.drop_database(SCRATCH_DB)
            with TestClient(create_app(settings)) as client:
                rows = run_pass(client, docs[:2] if index == 0 else docs)
            (print(f"warm-up done ({len(rows)} documents)") if index == 0 else passes.append(rows))
            if index:
                print(f"pass {index}/{args.passes} done")
    finally:
        mongo.drop_database(SCRATCH_DB)
        mongo.close()

    agg = aggregate(passes)
    preds = predictions(agg)
    results = RESULTS if args.variant == "v1" else RESULTS.with_name("pipeline_eval_v2.json")
    report = REPORT if args.variant == "v1" else REPORT.with_name("REPORT_v2.md")
    results.write_text(json.dumps({"variant": args.variant, "environment": env, "aggregate": agg, "predictions": preds, "passes": passes}, indent=1) + "\n", encoding="utf-8")
    report.write_text(render(agg, preds, env, passes[0], args.variant), encoding="utf-8")
    print(f"wrote {results.relative_to(ROOT)} and {report.relative_to(ROOT)}")
    for p in preds:
        print(("held   " if p["held"] else "FAILED ") + p["prediction"][:90], "|", p["detail"][:90])
    return 0


if __name__ == "__main__":
    sys.exit(main())
