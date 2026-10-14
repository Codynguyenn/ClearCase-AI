# Evaluation

Synthetic evaluation data, scoring scripts and (in Phase 6) the honest evaluation
report described in `docs/ClearCase_Cursor_Prompt.md`, section 11.

## What exists today

| File | What it is |
| --- | --- |
| `retrieval_dev.json` | **Development** questions for policy retrieval: 25 answerable (with the expected policy section) and 10 the policies do not address. Used to choose the similarity cutoff. Written by the same author as the fictional policies, so it is easier than real questions. |
| `tune_retrieval.py` | Measures hit@1, hit@3 and MRR (semantic vs semantic + keyword reorder) and sweeps the cutoff. Run: `backend/.venv/bin/python evaluation/tune_retrieval.py` |
| `results/retrieval_dev.json` | The output of that script for the pinned model revision, with denominators. Regenerate it after changing the model, the policies or the chunker. |

The headline numbers and how to read them are in `docs/decisions.md` (#30).

## What does NOT exist yet (Phase 6)

- A **held-out** retrieval question set. The dev set above must not be used to report
  final accuracy: the cutoff was tuned on it.
- The detection evaluation (precision/recall/F1 by category, pattern-only vs pattern +
  NER), export-verification outcomes, and processing latency.
- The evaluation report. Until it is written, nothing here should be quoted as
  real-world accuracy. All data in this project is synthetic.
