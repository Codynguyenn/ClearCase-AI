# Evaluation

Synthetic evaluation data, scoring scripts and (in Phase 6) the honest evaluation
report described in `docs/ClearCase_Cursor_Prompt.md`, section 11.

## What exists today

| File | What it is |
| --- | --- |
| `retrieval_dev.json` | **Development** questions for policy retrieval: 25 answerable (with the expected policy section) and 10 the policies do not address. Used to choose the cutoff. Same author as the policies, so easier than real questions. |
| `retrieval_heldout.json` + `.sha256` | **Held-out** set: 28 answerable, 16 unanswerable (including near-miss traps). Frozen by hash; never used for tuning. |
| `retrieval_heldout2.json` + `.sha256` | **Second held-out set** (95 questions: 36 answerable, 59 not addressed), written by a model that never saw the policies and labelled by two other models; frozen by hash; measured once. Built by `retrieval2/` (protocol first, then the build script, then `report.py`). |
| `tune_retrieval.py` | Dev only. hit@k / MRR and a cutoff sweep, in memory. |
| `eval_heldout.py` | Runs a frozen held-out set (`--set 2` for the second) through the **production** retriever against a scratch database. Refuses a changed file or a re-used label. `--smoke` runs the same code on the dev set and records nothing. |
| `compare_models.py` | Dev only. Compares small embedding models with the app's own pipeline. |
| `weak_tier_dev.py` | Dev only. Measures the reviewer-only "possibly related" tier. |
| `results/*.json` | Outputs, including `retrieval_heldout_runs.json` (every held-out run, with configuration, git state and a code fingerprint). |
| `assistant_eval/` | The **assistant** comparison (Haiku vs Sonnet vs Opus vs the app's detectors): `PROTOCOL.md` (rules and predictions, written first), `dataset.py` (seeded synthetic documents with ground truth), `metrics.py` (scoring), `run.py` (baselines and the live runner with a budget cap), `report.py` (every number in the report), `REPORT_heldout.md` (the result), `data/` (frozen held-out set + hash). |
| `pipeline_eval/` | The **pipeline** measurement: processing latency and export-verification outcomes on the 18 synthetic PDFs, with an independent re-read of every downloaded file. `PROTOCOL.md` (written first), `run.py`, `REPORT.md`. Free and deterministic (no model); `python evaluation/pipeline_eval/run.py`. |
| `results/assistant_*` | `assistant_runs_heldout.jsonl` (one record per run: tokens, cost, proposals, tools used, answer), `assistant_baselines_heldout.json`, `assistant_comparison_heldout.json`. The `_dev` files are harness debugging, not results. |

## Results (synthetic, same-author; not real-world accuracy)

All numbers are for the two fictional policies (19 passages) and `all-MiniLM-L6-v2`, cutoff 0.38.
Intervals are 95% Wilson intervals: with 16 to 28 questions they are wide, which is the honest picture.

| Measured on | Dev set (tuned on it) | **Held-out (never tuned on)** |
| --- | --- | --- |
| Right passage ranked first | 19/25 = 0.76 | **18/28 = 0.64** (0.46-0.79) |
| Right passage in top 3 | 22/25 = 0.88 | **21/28 = 0.75** (0.57-0.87) |
| Mean reciprocal rank | 0.822 | **0.733** |
| Answerable: relevant passage shown | 17/25 = 0.68 | **12/28 = 0.43** (0.27-0.61) |
| Answerable: told "no matching policy" | 6/25 = 0.24 | **13/28 = 0.46** (0.29-0.64) |
| Answerable: shown only wrong passages | 2/25 = 0.08 | **3/28 = 0.11** (0.04-0.27) |
| Unanswerable: correctly refused | 10/10 = 1.00 | **11/16 = 0.69** (0.44-0.86) |
| Unanswerable: wrongly shown a passage | 0/10 = 0.00 | **5/16 = 0.31** (0.14-0.56) |
| Balanced accuracy | 0.84 | **0.56** |
| Margin: cutoff minus best unanswerable score | +0.016 | **-0.193** |

**What happened.** The cutoff looked good on the set it was tuned on and did not carry over:
on unseen questions it refuses about half of the answerable ones and still lets through a third
of the unanswerable ones. That is overfitting to a small dev set, and it is why the held-out set
exists.

**Why (diagnosis made after both runs; not used to tune anything).**
- The five unanswerable questions that got passages are all *near-miss traps*: topics adjacent
  to the policies (reviewer training 0.57, audio/video 0.56, data-protection penalties 0.49,
  photographs 0.41, personal laptops 0.40). An embedding measures how similar the *topic* is,
  not whether the passage *answers the question*.
- For 8 of the 13 wrongly refused answerable questions the right passage was ranked 1st or 2nd;
  its score was just low (0.14-0.37) because scenario-style questions share few words with the
  policy text.
- The best-score ranges overlap almost completely: answerable median 0.40 (0.14-0.61),
  unanswerable median 0.31 (0.15-0.57). No cutoff can separate them.

**Conclusion.** Retrieval *ranking* is decent (the right passage is in the top 3 for about three
questions in four). The *decision* "does the policy answer this?" cannot be made reliably by a
similarity threshold. It needs something that reads the passage against the question (an LLM in
live mode, or a cross-encoder re-ranker; neither is evaluated here). Until then "no matching
policy" is a heuristic hint, and the app now says so.

### The two changes that were tried, under the rules fixed in advance

| Candidate | Dev-set outcome | Rule | Decision |
| --- | --- | --- | --- |
| `all-MiniLM-L12-v2` (Apache-2.0) | MRR 0.858, balanced 0.85, margin -0.040 | balanced +0.05, MRR +0.05, margin not smaller | not adopted |
| `bge-small-en-v1.5` (MIT, with its query instruction) | MRR 0.892, balanced 0.82, margin +0.002 | same | not adopted |
| `gte-small` (MIT) | MRR 0.893, balanced 0.74, margin -0.035 | same | not adopted |
| `multi-qa-MiniLM-L6-cos-v1` | not evaluated | licence must be stated on the model card | excluded: no licence stated |
| Reviewer-only "possibly related" tier | at L = 0.30: recovers 2/6 wrongly refused (33%), weak passages for 4/10 unanswerable (40%) | recover >= 30% and noise <= 50%; highest L that passes | **adopted, L = 0.30** |

Better models ranked better (MRR up to 0.893 vs 0.822) but did **not** make the refuse/answer
decision better (balanced accuracy equal or worse, margins thinner), which fits the diagnosis above.

On the held-out set, the adopted weak tier (final run) recovered a relevant passage for
**5 of 13** wrongly refused answerable questions (38%, CI 0.18-0.65) and showed weak passages for
**4 of 16** unanswerable questions (25%, CI 0.10-0.49). It met the same bar it was chosen under,
but the intervals are wide and the strict-cutoff numbers are unchanged (as they should be: the
baseline and final strict metrics are identical, which also confirms nothing else changed).
The tier is reviewer-only: it never reaches the assistant.

## Second held-out set for retrieval (model-written, model-labelled; run 2026-10-15)

Why: the first held-out set (44 questions) was written by the same author as the policies, was small, and had used its two allowed
runs. This set is **larger and more independent**: questions written by a model that was never shown the policies, each labelled
against the 19 passages by two other model runs, and kept only if both agreed. Protocol (prompts with hashes, selection rules, five
predictions) written first: `retrieval2/PROTOCOL.md`; full tables: `retrieval2/REPORT.md`. The retriever was run **once**, unchanged,
after the set was frozen by hash. Total spend $0.061.

| Measure | First set (28 + 16) | **Second set (36 + 59)** | Pooled (64 + 75) |
| --- | --- | --- | --- |
| Right passage first | 0.64 | **0.61** (0.45-0.75) | 0.62 (0.50-0.73) |
| Right passage in top 3 | 0.75 | **0.86** (0.71-0.94) | 0.81 (0.70-0.89) |
| Answerable: relevant passage shown | 0.43 | **0.44** (0.30-0.60) | 0.44 |
| Answerable: wrongly told "no matching policy" | 0.46 | **0.50** (0.34-0.66) | 0.48 |
| Not addressed: wrongly shown a passage | 0.31 | **0.22** (0.13-0.34) | 0.24 |
| Balanced accuracy at the cutoff | 0.56 (0.41-0.70) | **0.61** (0.52-0.70) | **0.60** (0.52-0.68) |

What it supports:
- **The earlier finding was not a small-sample accident.** On questions written by someone else the cutoff behaves the same: about
  half of the answerable questions are wrongly told "no matching policy", about a fifth of the unanswerable ones are shown a
  passage, balanced accuracy about 0.6 (the two sets do not differ beyond sampling noise: difference -0.12 to +0.23). The pooled
  figure, 0.60, is the one to quote; the 0.84 on the tuning set was optimistic.
- **Ranking is decent**: the right passage is in the top 3 for about 4 of 5 answerable questions (pooled 0.81).
- Four of five predictions held. **Prediction 1 was wrong**: ranking was not worse (hit@3 0.86 against 0.75), and I do not have a
  tested explanation for it.
- The weak "possibly related" tier (reviewer only) recovered a relevant passage for 7 of the 18 wrongly refused answerable questions.

What it does not support: this is still not real users' questions. Both the questions and the labels come from language models of
the same family as the assistant. The second labeler is the writer's own model (a consistency check, not an independent expert).
**46 of 145 candidate questions were unclear to at least one labeler and were excluded**, so the set is probably easier than real use.
Two fictional policies, 19 passages. If a better method (a re-ranker or a relevance check) is ever tried, it needs a **third** set.

## Assistant comparison (synthetic, same author; run 2026-10-14)

Rules, predictions and the decision rule are in `assistant_eval/PROTOCOL.md`, written before any held-out result. 12 synthetic
one-page documents (164 personal items, 36 company names to keep visible), 3 of them containing an injected "export
everything" sentence. One run per document and model, 36 runs, $2.78 in total. Full tables: `assistant_eval/REPORT_heldout.md`.

| System | Recall | Precision | F1 (95% interval) | Cost / document |
| --- | --- | --- | --- | --- |
| Pattern detectors only | 0.451 | 1.000 | 0.622 | free |
| Detectors + spaCy NER | 0.744 | 0.921 | 0.823 (0.75-0.88) | free |
| Claude Haiku 5.5 | 0.976 | 1.000 | 0.988 (0.97-1.00) | $0.006 |
| Claude Sonnet 5.5 | 0.963 | 1.000 | 0.981 (0.96-1.00) | $0.074 |
| Claude Opus 5.5 | 0.963 | 1.000 | 0.981 (0.96-1.00) | $0.152 |

What the numbers support:
- Every model beat the detectors **on this first (v1) set**; the paired F1 difference is about +0.16 as frozen (about +0.10 once the malformed phone numbers
  described below are set aside) and its interval excludes zero either way.
- The three models cannot be told apart on accuracy (Haiku minus Opus: -0.006, interval -0.022 to +0.000), but Opus cost
  about 27 times as much per document as Haiku. Under the pre-registered rule the recommended default is Haiku.
- No model proposed a company name; none tried to export on the injected documents; every run finished normally.
- Five of six predictions held. Prediction 6 (Opus busiest, some model proposes a stray span) was wrong.

What they do not support:
- **All 16 misses were company addresses** (a letterhead, a "Remit to" block). The labels called every address sensitive
  while the request asks for individuals' information, so the recall above is understated; with those 8 address lines left
  out (a post-hoc table, marked as such) all three models score 1.000. Detail in `PROTOCOL.md` (incident log) and decision 51.
- **A second data flaw, found later:** every synthetic phone number has a 3-digit last group, so none is a valid US number and
  the validating pattern detectors cannot match any of the 18 (their PHONE recall of 0 is an artefact, shown in the
  per-category table). Without those items the detectors' F1 is 0.876 rather than 0.823, and the model advantage about
  +0.10 (interval +0.04 to +0.18) rather than +0.16. Details: the report's second post-hoc section and the protocol's incident log.
- The task is easy, so it cannot separate strong models. 12 documents, one author, one run each, no run-to-run variance.
  Nothing here predicts accuracy on real documents.

Reproduce (the live step spends real money; the dry run and the report do not):

```bash
backend/.venv/bin/python evaluation/assistant_eval/run.py baselines --split heldout
backend/.venv/bin/python evaluation/assistant_eval/run.py live --split heldout --dry-run
backend/.venv/bin/python evaluation/assistant_eval/report.py --split heldout
```

## Pipeline measurement: latency and export-verification outcomes (synthetic; run 2026-10-15)

The real app (in-process) on the 18 synthetic PDFs (22 pages): upload, detect, a simulated careful reviewer approves exactly the correct
detector proposals, export with verification, download, and an **independent re-read of the downloaded file**. Protocol and four
predictions written first (`pipeline_eval/PROTOCOL.md`); full tables in `pipeline_eval/REPORT.md`. Free, no model; a warm-up and 3 passes.

| Measure | Result |
| --- | --- |
| Upload to "ready" (median, 90th percentile) | 47 ms (61 ms), about 45 ms per page |
| Detection: pattern detectors only / plus NER | 8 ms / 189 ms |
| Export: authorize, redact, verify, store | 186 ms (253 ms) |
| Exports verified with no warning | 9 of 18 |
| Exports verified but needing an acknowledgement | 9 of 18 |
| Failed or blocked exports; failed required checks | 0; 0 |
| Leftover copies of approved text in the downloaded files (independent check) | 9, and the verifier reported the same 9 |
| Kept company names preserved | 54 of 54 occurrences |

What it supports:
- **The verifier did its job.** Where the NER model proposed some occurrences of a name but missed one, the approved name stayed readable
  once in the file; the verifier flagged every such copy (9 of 9, none missed, none false) and held the download until acknowledged.
  Company names were never damaged. Nothing was redacted that should have stayed.
- Processing is fast on small documents (tens of milliseconds to extract, under 0.3 s to export and verify).
- Two predictions failed: I expected at least 15 clean exports and no leftover text; half the exports had a leftover copy, because
  detection missed some occurrences (PERSON coverage 75 of 108; EMAIL, SSN, ADDRESS fully covered).

What it does not support: real-document accuracy or speed. One author's synthetic one- and two-page documents; a simulated reviewer who
never errs; in-process timing on one machine (no network, no large files); the verifier checks only what was approved, so a person mention
nobody proposed (33 of 108 here) stays readable and unflagged unless a copy of the same text was approved. PHONE coverage is 0 of 27
only because the synthetic phone numbers are malformed (see the assistant comparison above).

## Corrected data (v2): what changed once the two data flaws were fixed (run 2026-10-15)

The first synthetic set (v1) had two flaws (malformed phone numbers; company addresses labelled as personal), found after the run. The
generator now has a **v1 variant that reproduces the old, measured data byte for byte** and a **v2 variant that fixes both**; new seeds, a
new frozen held-out set (`heldout_v2`, sha256 `90188e77...`), and `dataset.py` can no longer overwrite a frozen set. Protocol first:
`assistant_eval/PROTOCOL_V2.md`. Run on v2: the free detector baselines and pipeline measurement, and (with the owner's approval, $2.56) the three models.

| | v1 (as frozen) | v2 (corrected) |
| --- | --- | --- |
| Detectors, pattern plus NER: precision / recall / F1 | 0.921 / 0.744 / 0.823 | 0.895 / 0.942 / **0.918** |
| PHONE recall of the detectors | 0 of 18 | 18 of 18 |
| NER PERSON recall | 0.67 (48 of 72) | 0.88 (63 of 72) |
| Pipeline: exports clean / needing acknowledgement | 9 / 9 | 14 / 4 |
| Pipeline: leftover copies the verifier reported / found independently | 9 / 9 | 4 / 4 |
| Pipeline: upload to ready, export (median) | 47 ms, 186 ms | 55 ms, 208 ms |
| Models, F1: Haiku / Sonnet / Opus | 0.988 / 0.981 / 0.981 | 0.984 / 0.994 / 0.987 |
| Models' advantage over pattern plus NER (paired F1, 95% interval) | +0.16 (as frozen), +0.10 (flaws set aside) | **+0.07** (Haiku +0.066, +0.039 to +0.099; Sonnet +0.076; Opus +0.069) |
| Cost of the three-model run | $2.78 | $2.56 |

What it supports and what it changes:
- On clean data the models' advantage over the detectors is **about +0.07**, real for all three models (every interval excludes zero) but
  smaller than the +0.16 reported on the first set as frozen. All eight pre-registered predictions for the live run held; the three models
  cannot be told apart on accuracy (spread 0.010) and Opus costs 29 times Haiku per document, so the rule again recommends Haiku.
- NER's PERSON recall moved from 0.67 to 0.88 between two sets of 72 mentions drawn from the same lists: on synthetic data **the size of an
  advantage depends on the draw**. Quote ranges, not single numbers.
- Unchanged: the export verifier reported every leftover copy of approved text in both sets (13 of 13); latency is tens to a couple of hundred
  milliseconds on these small documents.
- Four predictions for the free steps (`PROTOCOL_V2.md`): two held, two failed (PERSON recall was not below 0.80; fewer than 6 exports needed an
  acknowledgement).

What it does not support: v2 is a re-draw with the flaws removed, using the same name lists and templates, so it is not a harder or more
varied set. Reports: `assistant_eval/REPORT_heldout_v2.md` and `pipeline_eval/REPORT_v2.md`.

## What does NOT exist yet (Phase 6)

- A retrieval question set **written by people** (the second set above is model-written and model-labelled, which is
  more independent than same-author but not human), and a **third** set before any new retrieval method is tuned or tested.
- A repeat of the assistant comparison on harder, longer, messier documents (v2 is a re-draw with the flaws removed, not a harder set),
  written before running anything. Until then nothing here should be quoted as real-world accuracy.
  All data in this project is synthetic.

## Held-out protocol for policy retrieval (written BEFORE any held-out run)

Why this exists: the cutoff and the keyword weight were tuned on `retrieval_dev.json`, so that
set cannot say how well retrieval works on questions it has not seen. The held-out set
(`retrieval_heldout.json`) is for exactly one purpose: an honest before/after measurement.

**Rules (fixed in advance, so results cannot be steered):**

1. **Same-author, and labelled as such.** The held-out questions were written by the same
   author as the policies (an AI assistant working with the project owner). They are written
   in a different voice from the dev set (scenarios, indirect phrasing, near-miss traps), but
   they are still easier and more aligned with the policies than real users' questions.
2. **Frozen.** `retrieval_heldout.json` is hashed (SHA-256) into `retrieval_heldout.sha256`.
   `eval_heldout.py` refuses to run if the file's hash does not match, so editing a question
   after seeing results is detectable.
3. **At most two runs, both recorded.** One run labelled `baseline` (today's behaviour,
   before any change) and one labelled `final` (after the change, if any). Every run is
   appended to `results/retrieval_heldout_runs.json` with its configuration and git commit;
   the script refuses a second run with the same label. No tuning on held-out results.
4. **Changes are chosen using the DEV set only**, and only if they meet these adoption
   rules, which were fixed before measuring:
   - *A different embedding model* is adopted only if, on the dev set, its best-cutoff
     balanced accuracy AND its MRR are each at least **0.05** higher than the current model's,
     and its margin between the cutoff and the highest-scoring unanswerable question is not
     smaller than the current model's (0.016). Candidates are small (<= ~35M parameters),
     permissively licensed models only; their model cards are checked first.
   - *A "possibly related" tier* (shown to the human reviewer only, never given to the
     assistant as policy) is adopted only if, on the dev set, it surfaces a relevant passage
     for at least **30%** of the answerable questions the strict cutoff wrongly refuses, while
     showing weak passages for at most **50%** of the unanswerable questions.
5. **Report everything**, including "no improvement", with denominators and 95% Wilson
   confidence intervals (small samples are wide; that is the honest picture).
6. **No peeking while choosing.** The baseline run prints aggregate numbers only. Per-question
   results are written to the results file but are not used to choose changes.
