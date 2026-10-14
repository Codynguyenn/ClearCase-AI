# Evaluation

Synthetic evaluation data, scoring scripts and (in Phase 6) the honest evaluation
report described in `docs/ClearCase_Cursor_Prompt.md`, section 11.

## What exists today

| File | What it is |
| --- | --- |
| `retrieval_dev.json` | **Development** questions for policy retrieval: 25 answerable (with the expected policy section) and 10 the policies do not address. Used to choose the cutoff. Same author as the policies, so easier than real questions. |
| `retrieval_heldout.json` + `.sha256` | **Held-out** set: 28 answerable, 16 unanswerable (including near-miss traps). Frozen by hash; never used for tuning. |
| `tune_retrieval.py` | Dev only. hit@k / MRR and a cutoff sweep, in memory. |
| `eval_heldout.py` | Runs the frozen held-out set through the **production** retriever against a scratch database. Refuses a changed file or a re-used label. `--smoke` runs the same code on the dev set and records nothing. |
| `compare_models.py` | Dev only. Compares small embedding models with the app's own pipeline. |
| `weak_tier_dev.py` | Dev only. Measures the reviewer-only "possibly related" tier. |
| `results/*.json` | Outputs, including `retrieval_heldout_runs.json` (every held-out run, with configuration, git state and a code fingerprint). |

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

## What does NOT exist yet (Phase 6)

- A **second, larger and more independent** retrieval question set (ideally written by people
  other than the policy author). The held-out set above has 44 questions from the same author.
- The detection evaluation (precision/recall/F1 by category, pattern-only vs pattern +
  NER), export-verification outcomes, and processing latency.
- The evaluation report. Until it is written, nothing here should be quoted as
  real-world accuracy. All data in this project is synthetic.

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
