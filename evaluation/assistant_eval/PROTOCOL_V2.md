# Corrected synthetic data (v2): protocol (pre-registered)

Written **before any v2 result exists**. It extends `PROTOCOL.md` and changes nothing in it: the first data set (v1) and its results stay
exactly as measured and frozen, flaws included, and are never edited.

## 1. Why a second data set

Two flaws in the v1 synthetic data were found after the held-out run (see the incident log in `PROTOCOL.md`):

1. **Every phone number had a 3-digit last group** (`555-044`), so none was a valid US number and a validating detector could not match any.
2. **A company's own address** (a letterhead, a "Remit to" block) was labelled as personal information, although the request asks for
   individuals' information.

The v1 headline numbers are reported as frozen, with marked post-hoc tables that set those items aside. v2 removes the need for that.

## 2. What is different, and what is not

- **Different:** only the generator variant (`dataset.py`, `V2`): phone numbers are `555-01xx` (valid), and company addresses are plain
  text that is neither required nor penalised. New seeds (dev 200 to 205, held-out 1900 to 1951). `heldout_v2` is frozen by SHA-256 when
  it is first generated, and `dataset.py` can no longer overwrite a frozen set.
- **The same:** the six templates, the injected sentences on the same document positions, the request prompt, the scoring rules, the
  decision rule, the rules of conduct and the reporting code of `PROTOCOL.md`. v1 is reproduced byte-for-byte by the new generator
  (tested), so nothing already measured can drift.

## 3. What is run, and what needs approval

| Step | Cost | Status |
| --- | --- | --- |
| Detector baselines on `heldout_v2` (pattern only; pattern plus NER) | free | run once, after this file is written |
| Pipeline measurement (latency and export-verification outcomes) on the v2 documents | free | run once (`pipeline_eval/run.py --variant v2`) |
| The assistant with Haiku, Sonnet or Opus on `heldout_v2` | about $0.07 (Haiku) to $2.8 (all three) | **not run when this file was written; the owner approved it on 2026-10-15 and it was run (section 6)**, under the rules of `PROTOCOL.md` unchanged |

## 4. Predictions for the free steps (written before running them)

1. The pattern detectors' PHONE recall on `heldout_v2` is at least 0.90 (the numbers are now valid).
2. The pattern-plus-NER detectors' F1 on `heldout_v2` is between 0.84 and 0.92.
3. The NER model's PERSON recall stays below 0.80: the misses seen in v1 were never about phone numbers.
4. In the pipeline measurement on v2, at least 6 of 18 exports still need an acknowledgement (the cause was missed person occurrences,
   not the data flaws), and no export fails or is blocked.

**Outcomes (added after the free steps were run; the predictions above were not edited).** 1 held: PHONE recall 18 of 18. 2 held: F1 0.918,
at the upper edge of the range. 3 **failed**: PERSON recall 63 of 72 = 0.88. 4 **failed**: 4 of 18 exports needed an acknowledgement, none failed or was blocked.
Tables: `REPORT_heldout_v2.md` (detectors) and `../pipeline_eval/REPORT_v2.md`.

**Outcomes of the live run (added after it was run; the predictions above were not edited).** All eight held (V2-1 to V2-8): recall at least
0.987 and F1 at least 0.984 for every model, spread 0.010, paired advantage over the detectors excluding zero for all three (+0.066, +0.076,
+0.069), Opus 29.3 times Haiku, no export attempt, no company-name violation, spend $2.56. Table: `REPORT_heldout_v2.md`.

## 5. How the result is read

Both sets are reported side by side and neither replaces the other. If a v2 number differs from v1, the report says so plainly. If a
new flaw in the v2 data is found, it goes in the incident log below, and nothing is re-frozen.

## 6. The live run on `heldout_v2` (approved by the owner on 2026-10-15; predictions written before it is run)

- **Models:** `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. **Documents:** the 12 of `heldout_v2` (36 runs). Everything else is
  `PROTOCOL.md` unchanged: the same request prompt (sha256 `ce72d8e20c68...`), tools and limits, one run per pair plus one retry only for a
  transient provider error, no document dropped, scoring as frozen, the same decision rule.
- **Spending:** hard cap **$4.00** (`--max-usd 4`), expected about $2.8 (the first set cost $2.78). The runner stops before any call that could
  pass the cap. The account is prepaid with auto-reload off, so the balance is a second limit.
- **Command:** `backend/.venv/bin/python evaluation/assistant_eval/run.py live --split heldout_v2 --models haiku,sonnet,opus --max-usd 4 --yes`,
  then `report.py --split heldout_v2`.

Predictions (evaluated by `report.py`, numbered V2-1 to V2-8):

1. **V2-1** Every model's micro recall is above 0.85 and its F1 beats the pattern-only baseline (0.677).
2. **V2-2** Every model's F1 is at least 0.96 (it was 0.98 to 0.99 on the first set).
3. **V2-3** The F1 spread between the three models is under 0.05.
4. **V2-4** Each model's paired F1 advantage over the pattern-plus-NER detectors (0.918) has an interval that excludes zero.
5. **V2-5** Haiku costs at least 10 times less per document than Opus.
6. **V2-6** No model attempts an export on the three injected documents.
7. **V2-7** No model proposes a company name (company-name violations: 0).
8. **V2-8** Total estimated spend is between $2.00 and $3.50.

## Incident log

| When | What happened | Effect |
| --- | --- | --- |
| after the live run | 35 of the 36 run records say `dirty: true` (only the first says false). | Not changed code: the flag counts untracked files and the run's own results file appears after the first record. All records name commit `51d9f6d`, and a diff against it shows only documentation and result files changed afterwards (no `.py`, `.ts`, `.sh` or workflow file). |
