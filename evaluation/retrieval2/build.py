"""Build the SECOND held-out question set for policy retrieval. Read PROTOCOL.md first.

    backend/.venv/bin/python evaluation/retrieval2/build.py plan              # free: what would be sent and what it would cost
    backend/.venv/bin/python evaluation/retrieval2/build.py write --yes       # a model that has NOT seen the policies writes candidate questions
    backend/.venv/bin/python evaluation/retrieval2/build.py label --yes       # two other models label each question against the policy passages
    backend/.venv/bin/python evaluation/retrieval2/build.py freeze            # free: keep the questions both labelers agree on, write the frozen file

Nothing here ever runs the retriever: the question set is fixed before the retriever has seen it, so it cannot be steered.
The questions are written by a model that is given only a general description of the two documents, never their text,
and labelled by two different models that are given the text. All calls are logged with token counts and cost in raw/.
The API key is read from the environment/.env and never printed or stored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
PROTOCOL = HERE / "PROTOCOL.md"
CANDIDATES = HERE / "candidates.json"
FROZEN = ROOT / "evaluation" / "retrieval_heldout2.json"
FROZEN_HASH = ROOT / "evaluation" / "retrieval_heldout2.sha256"
EXISTING = [ROOT / "evaluation" / "retrieval_dev.json", ROOT / "evaluation" / "retrieval_heldout.json"]
POLICY_DIR = ROOT / "sample_data" / "policies"

WRITER_MODEL = "claude-haiku-5-5"
LABELER_A = "claude-sonnet-5-5"
LABELER_B = "claude-haiku-5-5"
# USD per million tokens at list prices (input, output); the same table the assistant evaluation uses.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
CAP_USD = 0.20  # hard stop: the runner will not start a call that, at a realistic estimate, could pass this
PER_PERSONA = 25
LABEL_BATCH = 50
LABEL_EFFORT = "low"

PERSONAS = [
    "a first-year paralegal reviewing contracts for a client",
    "a senior associate preparing documents to send to opposing counsel",
    "a client-service manager at a small analytics company",
    "an IT and security analyst who supports the legal team",
    "an HR coordinator who handles offer letters and employee files",
    "a compliance officer answering a request from a regulator",
]

WRITER_SYSTEM = (
    "You write realistic test questions for an internal policy assistant. You have not seen the company's policy documents "
    "and must not guess or imitate their wording; write the questions real people would actually type."
)

WRITER_USER = """Setting: a small data-analytics company keeps an internal policy library. It has two documents: one about how \
documents are redacted (hiding personal information before a document is shared), and one about sharing documents outside \
the company and retaining them. Staff use a redaction tool and ask a policy assistant questions about these policies.

Write {n} different questions that {persona} might ask while working with contracts, offer letters, invoices, NDAs, leases \
or demand letters.

Mix these kinds, in no particular order:
- about two thirds are questions about redacting, sharing or retaining documents that the two documents above might plausibly settle;
- about one third are about nearby topics that such a library might NOT cover (for example employment law, IT security, \
privacy law in other countries, billing, contract drafting, litigation strategy, tool settings, document formats).

Vary the form: some very short, some a full sentence, some describing a concrete situation first. Do not label the kinds, \
do not explain, and do not repeat a question in different words. Do not start more than a few questions with the same word.

Output only a JSON array of {n} strings."""

LABELER_SYSTEM = (
    "You are a careful reviewer labelling test questions against a fixed set of policy passages. "
    "Use only the passages you are given; do not use outside knowledge about what such policies usually say."
)

LABELER_USER = """Policy passages (each is numbered; the number is its id):

{passages}

For each question below, decide:
- "A": answerable. One or more passages contain a statement that directly settles the question (a rule, requirement, \
permission or prohibition that an assistant could quote as the answer). List the numbers of the passages that contain it (at most three).
- "N": not addressed. The passages do not settle the question: the topic is absent, only loosely related, or a passage \
mentions it only to say the policy does not cover it. Use [] for the numbers.
- "U": unclear. The passages partly address it, or the answer depends on facts the question does not give, so careful \
readers could disagree. List the best-matching passage numbers.

Be strict about "A": a passage that is merely on the same topic is not enough.

Questions:
{questions}

Output only a JSON array with one object per question, in order, like:
[{{"id": "c001", "v": "A", "p": [3]}}, {{"id": "c002", "v": "N", "p": []}}]"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hashes() -> dict[str, str]:
    return {
        "writer": sha256_text(WRITER_SYSTEM + "\n\n" + WRITER_USER),
        "labeler": sha256_text(LABELER_SYSTEM + "\n\n" + LABELER_USER),
    }


# ------------------------------------------------------------------------------------------------ corpus
@dataclass(frozen=True)
class Passage:
    number: int  # 1-based id shown to the labelers
    policy_id: str
    section: str
    text: str


def load_corpus() -> list[Passage]:
    """The production chunks of the two sample policies, in the order the app stores them (no embeddings are needed)."""
    from app.services.policy_chunking import chunk_markdown, parse_front_matter, slugify

    out: list[Passage] = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8-sig"))
        policy_id = meta.get("policy_id") or slugify(path.stem)
        for chunk in chunk_markdown(body):
            out.append(Passage(len(out) + 1, policy_id, chunk.section, chunk.text))
    return out


def passages_block(corpus: Sequence[Passage]) -> str:
    return "\n\n".join(f"[{p.number}] {p.policy_id}, section \"{p.section}\":\n{p.text}" for p in corpus)


# ------------------------------------------------------------------------------------------------ parsing and selection (pure)
def parse_json_array(text: str) -> list:
    """The model was asked for a bare JSON array; tolerate a code fence around it, nothing else."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end < start:
        raise ValueError("no JSON array in the reply")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, list):
        raise ValueError("the reply is not a JSON array")
    return value


_STOP = {"the", "and", "for", "are", "can", "does", "our", "you", "that", "this", "with", "what", "when", "how", "should", "have", "from"}


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in _STOP}


def jaccard(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def dedupe(candidates: Sequence[dict], existing: Sequence[str], within: float = 0.7, against: float = 0.6) -> tuple[list[dict], dict[str, int]]:
    """Drop repeats inside the candidate list and near-copies of questions in the earlier sets. Counts are reported."""
    kept: list[dict] = []
    dropped = {"empty_or_too_long": 0, "repeat": 0, "copy_of_earlier_set": 0}
    for cand in candidates:
        q = cand["q"].strip()
        if not q or len(q) > 400:
            dropped["empty_or_too_long"] += 1
        elif any(jaccard(q, k["q"]) >= within for k in kept):
            dropped["repeat"] += 1
        elif any(jaccard(q, e) >= against for e in existing):
            dropped["copy_of_earlier_set"] += 1
        else:
            kept.append({**cand, "q": q})
    return kept, dropped


@dataclass(frozen=True)
class Label:
    verdict: str  # A, N or U
    passages: tuple[int, ...]
    why: str


def parse_labels(items: Sequence, expected_ids: Sequence[str], n_passages: int) -> dict[str, Label]:
    """Valid labels only. Anything malformed is left out (the question is then excluded as 'missing label')."""
    wanted = set(expected_ids)
    out: dict[str, Label] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        qid, verdict, nums = item.get("id"), item.get("v"), item.get("p", [])
        if qid not in wanted or qid in out or verdict not in ("A", "N", "U") or not isinstance(nums, list):
            continue
        if not all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= n_passages for n in nums):
            continue
        if verdict == "A" and not nums:
            continue
        out[qid] = Label(verdict, tuple(sorted(set(nums))), str(item.get("why", ""))[:200])
    return out


def combine(a: dict[str, Label], b: dict[str, Label], corpus: Sequence[Passage], order: Sequence[str]) -> tuple[list[dict], dict[str, int]]:
    """Keep a question only if both labelers gave the same definite verdict (and, for answerable ones, named a common passage).

    For an answerable question the relevant sections are the passages BOTH labelers named (the intersection), so a hit
    requires agreement, never the union. Returns the decided questions and a count per exclusion reason."""
    by_number = {p.number: p for p in corpus}
    kept: list[dict] = []
    excluded = {"missing_label": 0, "unclear": 0, "labelers_disagree_on_verdict": 0, "labelers_name_different_passages": 0}
    for qid in order:
        la, lb = a.get(qid), b.get(qid)
        if la is None or lb is None:
            excluded["missing_label"] += 1
        elif "U" in (la.verdict, lb.verdict):
            excluded["unclear"] += 1
        elif la.verdict != lb.verdict:
            excluded["labelers_disagree_on_verdict"] += 1
        elif la.verdict == "N":
            kept.append({"id": qid, "answerable": False, "relevant": []})
        else:
            common = sorted(set(la.passages) & set(lb.passages))
            if not common:
                excluded["labelers_name_different_passages"] += 1
            else:
                sections = sorted({(by_number[n].policy_id, by_number[n].section) for n in common})
                kept.append({"id": qid, "answerable": True, "relevant": [list(s) for s in sections]})
    return kept, excluded


# ------------------------------------------------------------------------------------------------ the calls (spend money)
class Budget:
    def __init__(self, cap: float) -> None:
        self.cap, self.spent = cap, 0.0

    def can_start(self, estimate: float) -> bool:
        return self.spent + estimate <= self.cap

    def add(self, cost: float) -> None:
        self.spent += cost


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES[model]
    return (input_tokens * price_in + output_tokens * price_out) / 1e6


def estimate_usd(model: str, prompt_chars: int, expected_output_tokens: int) -> float:
    """A realistic (not worst-case) estimate: 4 characters per token, with a margin of 50% on the output."""
    return cost_usd(model, prompt_chars // 4, int(expected_output_tokens * 1.5))


def check_protocol_matches_prompts() -> None:
    text = PROTOCOL.read_text(encoding="utf-8") if PROTOCOL.exists() else ""
    for name, digest in prompt_hashes().items():
        if digest not in text:
            raise SystemExit(f"REFUSING TO SPEND: PROTOCOL.md does not contain the sha256 of the {name} prompt ({digest[:12]}...).\n"
                             "The protocol must be written, with these hashes, before any question is generated.")


def make_client():
    import anthropic

    from app.config import Settings

    settings = Settings()
    if settings.llm_provider.strip().lower() != "anthropic" or not settings.llm_api_key:
        raise SystemExit("Not configured: set CLEARCASE_LLM_PROVIDER=anthropic and CLEARCASE_LLM_API_KEY in .env.")
    return anthropic.Anthropic(api_key=settings.llm_api_key, max_retries=2, timeout=300)


def call_model(client, model: str, system: str, user: str, max_tokens: int, effort: str | None) -> dict:
    import anthropic

    kwargs = {"output_config": {"effort": effort}} if effort else {}
    try:
        response = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}], **kwargs)
    except anthropic.AnthropicError as exc:  # never echo the message: it may contain parts of the request
        raise SystemExit(f"Provider call failed: {type(exc).__name__} (status {getattr(exc, 'status_code', None)}).") from None
    text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        raise SystemExit("The reply was cut off (max_tokens). Nothing was saved for this call; no retry is attempted automatically.")
    usage = response.usage
    return {
        "model": model, "text": text, "request_id": getattr(response, "_request_id", None), "stop_reason": response.stop_reason,
        "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens, "cost_usd": cost_usd(model, usage.input_tokens, usage.output_tokens),
    }  # fmt: skip


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def existing_questions() -> list[str]:
    out: list[str] = []
    for path in EXISTING:
        data = json.loads(path.read_text(encoding="utf-8"))
        out += [item["q"] for item in data.get("answerable", []) + data.get("unanswerable", [])]
    return out


# ------------------------------------------------------------------------------------------------ stages
def stage_plan() -> None:
    corpus = load_corpus()
    print(f"policy passages: {len(corpus)} ({len({p.policy_id for p in corpus})} policies)")
    w = estimate_usd(WRITER_MODEL, len(WRITER_SYSTEM + WRITER_USER), PER_PERSONA * 25)
    n_cand = len(PERSONAS) * PER_PERSONA
    batches = -(-n_cand // LABEL_BATCH)
    prefix = len(LABELER_SYSTEM + LABELER_USER + passages_block(corpus))
    lab = lambda m: batches * estimate_usd(m, prefix + LABEL_BATCH * 120, LABEL_BATCH * 22)  # noqa: E731
    print(f"writer  {WRITER_MODEL}: {len(PERSONAS)} calls x {PER_PERSONA} questions = {n_cand} candidates, about ${len(PERSONAS) * w:.3f}")
    print(f"labeler {LABELER_A}: {batches} calls, about ${lab(LABELER_A):.3f}")
    print(f"labeler {LABELER_B}: {batches} calls, about ${lab(LABELER_B):.3f}  (same model as the writer; it only gates agreement)")
    print(f"expected total about ${len(PERSONAS) * w + lab(LABELER_A) + lab(LABELER_B):.2f}; hard cap ${CAP_USD:.2f}")
    print("prompt hashes:", {k: v[:12] + "..." for k, v in prompt_hashes().items()})


def stage_write(yes: bool) -> None:
    check_protocol_matches_prompts()
    done = read_json(RAW / "writer.json", {"calls": []})
    finished = {c["persona"] for c in done["calls"]}
    todo = [p for p in PERSONAS if p not in finished]
    print(f"{len(finished)} persona(s) already written; {len(todo)} to go")
    if not todo or not yes:
        if todo:
            print("Add --yes to spend (about a cent).")
        return
    client, budget = make_client(), Budget(CAP_USD)
    budget.add(sum(c["cost_usd"] for c in done["calls"]))
    for persona in todo:
        user = WRITER_USER.format(n=PER_PERSONA, persona=persona)
        estimate = estimate_usd(WRITER_MODEL, len(WRITER_SYSTEM + user), PER_PERSONA * 25)
        if not budget.can_start(estimate):
            raise SystemExit(f"BUDGET CAP: stopping before '{persona}' (spent ${budget.spent:.3f}).")
        reply = call_model(client, WRITER_MODEL, WRITER_SYSTEM, user, max_tokens=3000, effort=None)
        questions = [q for q in parse_json_array(reply["text"]) if isinstance(q, str)]
        done["calls"].append({"persona": persona, "questions": questions, **{k: v for k, v in reply.items() if k != "text"}})
        budget.add(reply["cost_usd"])
        RAW.mkdir(exist_ok=True)
        (RAW / "writer.json").write_text(json.dumps(done, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  {persona[:50]:50} {len(questions)} questions  ${reply['cost_usd']:.4f}")
    # candidates: deduplicated, with stable ids
    flat = [{"q": q, "persona": c["persona"]} for c in done["calls"] for q in c["questions"]]
    kept, dropped = dedupe(flat, existing_questions())
    for i, cand in enumerate(kept, start=1):
        cand["id"] = f"c{i:03d}"
    CANDIDATES.write_text(json.dumps({"prompt_sha256": prompt_hashes()["writer"], "model": WRITER_MODEL, "written": len(flat), "dropped": dropped, "candidates": kept}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(flat)} written, {len(kept)} kept after de-duplication {dropped}; spent ${budget.spent:.3f}")


def stage_label(yes: bool) -> None:
    check_protocol_matches_prompts()
    cands = read_json(CANDIDATES, None)
    if cands is None:
        raise SystemExit("Run the write stage first.")
    corpus, items = load_corpus(), cands["candidates"]
    block = passages_block(corpus)
    client, budget = (make_client() if yes else None), Budget(CAP_USD)
    for model, seed in ((LABELER_A, 1), (LABELER_B, 2)):
        path = RAW / f"labels_{model}.json"
        out = read_json(path, {"model": model, "prompt_sha256": prompt_hashes()["labeler"], "batches": []})
        budget.add(sum(b["cost_usd"] for b in out["batches"]))
        order = list(items)
        random.Random(seed).shuffle(order)  # a different order per labeler, fixed by the seed
        for start in range(0, len(order), LABEL_BATCH):
            batch = order[start : start + LABEL_BATCH]
            key = [c["id"] for c in batch]
            if any(b["ids"] == key for b in out["batches"]):
                continue
            if not yes:
                print(f"{model}: batch {start // LABEL_BATCH + 1} not done yet (add --yes to spend)")
                continue
            user = LABELER_USER.format(passages=block, questions="\n".join(f"{c['id']}: {c['q']}" for c in batch))
            estimate = estimate_usd(model, len(LABELER_SYSTEM + user), len(batch) * 22)
            if not budget.can_start(estimate):
                raise SystemExit(f"BUDGET CAP: stopping before {model} batch {start // LABEL_BATCH + 1} (spent ${budget.spent:.3f}).")
            reply = call_model(client, model, LABELER_SYSTEM, user, max_tokens=8000, effort=LABEL_EFFORT)
            out["batches"].append({"ids": key, "raw_text": reply["text"], **{k: v for k, v in reply.items() if k != "text"}})
            budget.add(reply["cost_usd"])
            RAW.mkdir(exist_ok=True)
            path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"  {model} batch {start // LABEL_BATCH + 1}: {len(batch)} questions  ${reply['cost_usd']:.4f}  (total ${budget.spent:.3f})")


def load_labels(model: str, corpus: Sequence[Passage]) -> dict[str, Label]:
    data = read_json(RAW / f"labels_{model}.json", None)
    if data is None:
        raise SystemExit(f"No labels from {model} yet.")
    labels: dict[str, Label] = {}
    for batch in data["batches"]:
        try:
            labels.update(parse_labels(parse_json_array(batch["raw_text"]), batch["ids"], len(corpus)))
        except ValueError:
            pass  # an unparsable batch leaves its questions without a label: they are excluded and counted
    return labels


def stage_freeze() -> None:
    if FROZEN_HASH.exists():
        raise SystemExit(f"Already frozen ({FROZEN_HASH.name}). The set is never rebuilt after the retriever has seen it.")
    corpus, cands = load_corpus(), read_json(CANDIDATES, None)
    if cands is None:
        raise SystemExit("Run the write and label stages first.")
    a, b = load_labels(LABELER_A, corpus), load_labels(LABELER_B, corpus)
    order = [c["id"] for c in cands["candidates"]]
    decided, excluded = combine(a, b, corpus, order)
    by_id = {c["id"]: c for c in cands["candidates"]}
    answerable = [{"id": f"s{i:02d}", "q": by_id[d["id"]]["q"], "relevant": d["relevant"], "persona": by_id[d["id"]]["persona"]} for i, d in enumerate([d for d in decided if d["answerable"]], start=1)]
    unanswerable = [{"id": f"su{i:02d}", "q": by_id[d["id"]]["q"], "persona": by_id[d["id"]]["persona"]} for i, d in enumerate([d for d in decided if not d["answerable"]], start=1)]
    both = [x for x in order if x in a and x in b]
    agree = sum(a[x].verdict == b[x].verdict for x in both)
    spent = sum(c["cost_usd"] for c in read_json(RAW / "writer.json", {"calls": []})["calls"])
    spent += sum(batch["cost_usd"] for m in (LABELER_A, LABELER_B) for batch in read_json(RAW / f"labels_{m}.json", {"batches": []})["batches"])
    dataset = {
        "_about": "SECOND held-out set for policy retrieval. FROZEN: its SHA-256 is in retrieval_heldout2.sha256 and eval_heldout.py --set 2 "
                  "refuses to run if the file changed. Never use it to tune anything. The questions were written by a language model that was "
                  "NOT shown the policies, and each was labelled against the policy passages by two other models; a question is kept only if "
                  "both agreed. This is more independent than the first held-out set, but it is not human-written and not real users' "
                  "questions. See evaluation/retrieval2/PROTOCOL.md.",
        "policies": sorted({p.policy_id for p in corpus}),
        "_provenance": {
            "writer": WRITER_MODEL, "labelers": [LABELER_A, LABELER_B], "prompt_sha256": prompt_hashes(),
            "candidates_written": cands["written"], "dropped_as_repeats": cands["dropped"], "candidates_labelled": len(order),
            "labelers_agree_on_verdict": f"{agree}/{len(both)}", "excluded": excluded, "kept": len(decided), "spent_usd": round(spent, 4),
        },  # fmt: skip
        "answerable": answerable,
        "unanswerable": unanswerable,
    }
    FROZEN.write_text(json.dumps(dataset, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    digest = hashlib.sha256(FROZEN.read_bytes()).hexdigest()
    FROZEN_HASH.write_text(digest + "\n", encoding="utf-8")
    print(f"frozen {FROZEN.name}: {len(answerable)} answerable, {len(unanswerable)} unanswerable; excluded {excluded}")
    print(f"labelers agreed on the verdict for {agree}/{len(both)}; spent ${spent:.3f}; sha256 {digest}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("stage", choices=["plan", "write", "label", "freeze"])
    parser.add_argument("--yes", action="store_true", help="actually spend money (a few cents)")
    args = parser.parse_args()
    {"plan": stage_plan, "write": lambda: stage_write(args.yes), "label": lambda: stage_label(args.yes), "freeze": stage_freeze}[args.stage]()


if __name__ == "__main__":
    main()
