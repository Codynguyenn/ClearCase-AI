"""Scoring rules for redaction proposals (pre-registered in PROTOCOL.md; do not change after the held-out freeze).

Ground truth is a list of items on pages: SENSITIVE items (PERSON, EMAIL, PHONE, SSN, ADDRESS) that should be redacted, and KEEP items
(company names) that the task says must stay visible. A proposal is a character span on a page with the label the proposer gave it.

Rules, in plain words:

* A proposal is ATTRIBUTED to the ground-truth item it overlaps most, if that overlap is at least 50% of the proposal's own length.
  - attributed to a sensitive item  -> a correct proposal (true positive)
  - attributed to a KEEP item       -> a violation (a false positive that broke the instruction "keep company names visible")
  - not attributed to anything      -> an extra (a false positive)
* A sensitive item is FOUND if the union of all proposals on its page covers at least 80% of its characters.
  This lets two proposals that together cover a two-line address count, and a proposal that is a little longer than the item still count.
* Precision = correct proposals / all proposals. Recall = found items / sensitive items. F1 is their harmonic mean.
* Recall is also reported per ground-truth category; precision per proposal LABEL (what the proposer called it).
* Everything is counted per occurrence (the same name three times is three items) and per document, then summed. Intervals come from
  resampling DOCUMENTS (bootstrap), because documents, not items, are the independent units.

The labels the proposer gives are not used to decide correctness: only where the span is. That keeps the score about "did it find the
right text", not about naming conventions.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

SENSITIVE = ("PERSON", "EMAIL", "PHONE", "SSN", "ADDRESS")
ATTRIBUTION_MIN = 0.5  # share of a proposal that must lie inside a ground-truth item
FOUND_MIN = 0.8  # share of a sensitive item that proposals must cover


@dataclass(frozen=True)
class Item:
    page: int
    start: int
    end: int
    kind: str  # PERSON EMAIL PHONE SSN ADDRESS ORG
    keep: bool = False


@dataclass(frozen=True)
class Proposal:
    page: int
    start: int
    end: int
    label: str = ""


@dataclass
class DocScore:
    gt: Counter = field(default_factory=Counter)  # sensitive items per category
    found: Counter = field(default_factory=Counter)  # ... of which found
    keep_items: int = 0
    keep_covered: int = 0  # KEEP items at least half covered by proposals
    proposals: int = 0
    correct: int = 0  # proposals attributed to a sensitive item
    keep_violations: int = 0  # proposals attributed to a KEEP item
    extras: int = 0  # proposals attributed to nothing
    by_label_total: Counter = field(default_factory=Counter)
    by_label_correct: Counter = field(default_factory=Counter)


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _union_cover(item: Item, spans: Sequence[Proposal]) -> int:
    """Characters of ``item`` covered by the union of the proposals on its page."""
    clipped = sorted(
        (max(p.start, item.start), min(p.end, item.end)) for p in spans if p.page == item.page and _overlap(p.start, p.end, item.start, item.end) > 0
    )
    covered, edge = 0, item.start
    for start, end in clipped:
        start = max(start, edge)
        if end > start:
            covered += end - start
            edge = end
    return covered


def score_document(truth: Sequence[Item], proposals: Sequence[Proposal]) -> DocScore:
    score = DocScore()
    for item in truth:
        if item.keep:
            score.keep_items += 1
            if _union_cover(item, proposals) >= ATTRIBUTION_MIN * (item.end - item.start):
                score.keep_covered += 1
        else:
            score.gt[item.kind] += 1
            if _union_cover(item, proposals) >= FOUND_MIN * (item.end - item.start):
                score.found[item.kind] += 1

    for p in proposals:
        score.proposals += 1
        score.by_label_total[p.label or "UNLABELLED"] += 1
        length = max(p.end - p.start, 1)
        best, best_overlap = None, 0
        for item in truth:
            if item.page == p.page:
                ov = _overlap(p.start, p.end, item.start, item.end)
                if ov > best_overlap:
                    best, best_overlap = item, ov
        if best is None or best_overlap < ATTRIBUTION_MIN * length:
            score.extras += 1
        elif best.keep:
            score.keep_violations += 1
        else:
            score.correct += 1
            score.by_label_correct[p.label or "UNLABELLED"] += 1
    return score


# ------------------------------------------------------------------------------------------------ aggregation
def ratio(n: float, d: float) -> float | None:
    return None if d == 0 else n / d


def f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean. No proposals at all (precision undefined) with items to find scores 0: it found nothing."""
    if recall is None:
        return None  # nothing to find in the first place
    if precision is None or precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def summarise(scores: Sequence[DocScore]) -> dict:
    gt, found = Counter(), Counter()
    by_total, by_correct = Counter(), Counter()
    proposals = correct = violations = extras = keep_items = keep_covered = 0
    for s in scores:
        gt.update(s.gt)
        found.update(s.found)
        by_total.update(s.by_label_total)
        by_correct.update(s.by_label_correct)
        proposals += s.proposals
        correct += s.correct
        violations += s.keep_violations
        extras += s.extras
        keep_items += s.keep_items
        keep_covered += s.keep_covered
    total_gt, total_found = sum(gt.values()), sum(found.values())
    precision, recall = ratio(correct, proposals), ratio(total_found, total_gt)
    return {
        "documents": len(scores),
        "proposals": proposals,
        "correct": correct,
        "keep_violations": violations,
        "extras": extras,
        "sensitive_items": total_gt,
        "found": total_found,
        "precision": precision,
        "recall": recall,
        "f1": f1(precision, recall),
        "keep_items": keep_items,
        "keep_items_covered": keep_covered,
        "recall_by_category": {c: {"found": found[c], "of": gt[c], "recall": ratio(found[c], gt[c])} for c in SENSITIVE},
        "precision_by_label": {
            label: {"correct": by_correct[label], "of": by_total[label], "precision": ratio(by_correct[label], by_total[label])}
            for label in sorted(by_total)
        },
    }


def micro_f1(scores: Sequence[DocScore]) -> float | None:
    return summarise(scores)["f1"]


def bootstrap(per_doc: Sequence[DocScore], stat: Callable[[Sequence[DocScore]], float | None], n: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    """95% interval for ``stat`` by resampling documents with replacement."""
    if not per_doc:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(n):
        sample = [per_doc[rng.randrange(len(per_doc))] for _ in per_doc]
        v = stat(sample)
        if v is not None:
            values.append(v)
    if not values:
        return None
    values.sort()
    return values[int(0.025 * len(values))], values[min(int(0.975 * len(values)), len(values) - 1)]


def paired_difference(a: Sequence[DocScore], b: Sequence[DocScore], stat: Callable[[Sequence[DocScore]], float | None] = micro_f1, n: int = 2000, seed: int = 11) -> dict | None:
    """stat(a) - stat(b) on the SAME resampled documents. ``a[i]`` and ``b[i]`` must be the same document."""
    if len(a) != len(b) or not a:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        idx = [rng.randrange(len(a)) for _ in a]
        sa, sb = stat([a[i] for i in idx]), stat([b[i] for i in idx])
        if sa is not None and sb is not None:
            diffs.append(sa - sb)
    base_a, base_b = stat(a), stat(b)
    if not diffs or base_a is None or base_b is None:
        return None
    diffs.sort()
    low, high = diffs[int(0.025 * len(diffs))], diffs[min(int(0.975 * len(diffs)), len(diffs) - 1)]
    return {"difference": base_a - base_b, "low": low, "high": high, "excludes_zero": low > 0 or high < 0}
