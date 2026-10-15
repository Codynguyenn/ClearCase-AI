# Pipeline measurement: processing latency and export-verification outcomes (pre-registered)

Written **before the measurement was run**. It answers the two items the specification's evaluation section asks for and the earlier
evaluations did not cover: *processing latency* and *supported export-verification outcomes*. It is deterministic software measured on
synthetic documents. No language model and no spending are involved. Nothing here is real-document performance.

## 1. Questions

1. How long do the stages of the real pipeline take on these documents (upload to "ready", detection, export with verification)?
2. When a careful reviewer approves exactly the detector proposals that point at personal information, what does the export
   verification say, how often does it need an acknowledgement, and is the approved text really gone from the downloaded file
   while the kept text (company names) is still there?

## 2. What is run

- **Documents:** the 18 synthetic PDFs of the assistant evaluation (6 development, 12 held-out; 6 templates; 1 to 2 pages each) with
  their ground-truth spans. No tuning of anything happens here, so using the held-out files does not compromise them.
- **Path:** the real FastAPI app (in-process `TestClient`) against a real MongoDB scratch database and the real PyMuPDF and spaCy:
  upload, wait until ready, `propose-detected` for PERSON, EMAIL, PHONE, SSN, ADDRESS (pattern detectors plus NER, as the app does),
  review, `export-authorizations`, `exports`, download. In-process means no network hop; real deployments add that.
- **Reviewer (simulated):** a proposal is **approved** if the evaluation's matching rule attributes it to a sensitive ground-truth
  item (at least 50% of the proposal lies inside the item, the same rule as the assistant evaluation); otherwise **rejected**. If the export then needs an acknowledgement (copies of approved
  text remain that no finding covers), the reviewer acknowledges so the file can be inspected, and the need is counted.
- **Repetitions:** a warm-up document (not counted: it loads the NER model), then 3 full passes over the 18 documents, each pass on a
  fresh database. Latencies are the median over the passes per document, then summarised over documents (median, 90th percentile, max).
  All raw times are saved. Latency is for this machine and for one- and two-page documents; it says nothing about large files.

## 3. Outcomes and denominators (defined now)

For each document and then summed over documents:

- **Verification outcome:** `verified` with no warnings, `verified` but needing acknowledgement, `verification_failed`, or `blocked`.
  Denominator: 18 documents.
- **Approved text still readable:** after export, the number of occurrences of approved text still present in the downloaded file's
  extracted text, ignoring occurrences inside kept company names. Denominator: approved findings.
- **Kept text preserved:** for every distinct company name in the ground truth, the number of its occurrences in the output divided by
  the number in the original (capped at 1). Denominator: occurrences of company names in the originals.
- **Personal items covered by an approval** (80% coverage, as in the assistant evaluation) over all personal items. This is the
  detectors' recall seen at the end of the pipeline; **the export verifier cannot know about items nobody proposed**, and the report
  says so.
- **Latency per stage** in milliseconds, and per page.

## 4. Predictions (written before the run)

1. At least 15 of the 18 exports are `verified` without needing an acknowledgement; none is `verification_failed` or `blocked`.
2. Approved text still readable in the downloaded files: **0** occurrences. Kept company names preserved: 100%.
3. On this machine the median upload-to-ready is under 1 s per document and the median export (authorize, redact, verify) under 2 s.
4. Whenever an export needs an acknowledgement, the cause is a detector that proposed some occurrences of a text but not all of them.

## 5. Rules of conduct

No change to the documents, the reviewer rule or the outcome definitions after this file is written. The measurement is run once
(warm-up plus 3 passes); a bug in the measurement script is fixed and logged below, and the affected measurement repeated and both
reported. Nothing is dropped after the fact.

## 6. Known limits

Synthetic, small, deterministic documents of one author. A simulated reviewer who never errs: a real reviewer makes mistakes the
verifier cannot see. In-process timing on one machine, one pass count, one browser-less path. The verifier checks what was approved;
it cannot find sensitive text nobody proposed. Scanned, mixed and encrypted PDFs are covered by the test suite, not measured here.

## Incident log

| When | What happened | Effect |
| --- | --- | --- |
| before any run | The protocol said each approval uses "apply to all matching"; the script approves by finding id instead (every proposal pointing at personal information is approved, so "all matching" would add nothing). | Sentence removed before the first run; no result existed. |
| after the first run | The first run reported personal-item coverage of 186 of 246 with no breakdown. The assistant evaluation had meanwhile revealed that the synthetic phone numbers are malformed, so a per-category breakdown was added to the script and the measurement **repeated once**. | Outcomes were identical in both runs (9 verified, 9 needing acknowledgement, 9 leftover copies, 54 of 54 kept names, 196 approved findings); only the timings differ slightly (median upload-to-ready 49 then 47 ms, export 201 then 186 ms). The report is from the second run. |
