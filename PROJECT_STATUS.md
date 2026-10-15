# ClearCase — project status

Last updated: 2026-10-15 (**all six phases complete.** Merged to `main` through PR #11; the polish PR and the pipeline-measurement PR are the last two open)

This file is the hand-off point for any new session. Read it first, then
`docs/ClearCase_Cursor_Prompt.md` (the specification) and `docs/decisions.md`.

## Phase progress

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Repository setup, MongoDB, upload, extraction, PDF viewing | **Complete and verified** |
| 2 | Search, candidate detection, manual review, real export, verification | **Complete and verified** |
| 3 | Real MCP server/client integration with tested tools | **Complete and verified** (merged to `main`) |
| 4 | Policy ingestion, embeddings, retrieval, source citations | **Complete and verified** (PR #3 and the hardening bundle, PR #5, merged to `main`) |
| 5 | Live agent integration, persisted chat, visible tool activity | **Complete and verified** (PR #6, merged to `main`) |
| 6 | Integration tests, evaluation, interface polish, documentation | **Complete and verified.** Assistant model comparison, browser end-to-end test, second retrieval question set, pipeline latency and export-verification measurement, bulk-button fix, demo script, interview guide, Compose re-run, README screenshots. Optional follow-ups are listed under "Remaining work" |

## Working features (verified)

Everything from Phase 1, plus:

Search (`backend/app/services/search.py`, `GET /documents/{id}/search`):
- Literal match (query escaped; whitespace matches any run of spaces/newlines).
- Case-sensitive and whole-word options; query length and result count capped.
- Stable occurrence ids; multi-line hits produce one box per line.
- Unmappable / partial-word spans flagged; unsupported pages listed so
  "0 results" is not mistaken for "not in the document".

Detection (`backend/app/services/detection.py`):
- Patterns: email, US phone (separators required; NANP rules), SSN-format
  (SSA area/group/serial rules), street address, `City, ST ZIP`.
- Validators drop implausible matches; passing means "format valid", not
  "verified identifier". Bare numbers are not treated as accounts/phones.
- spaCy `en_core_web_sm` NER for PERSON / ORG / GPE-LOC with model version in
  the source string and `review_needed` (no fake probabilities).
- Continues with patterns only if the NER model is not installed, and says so.
- Detection stores **nothing**. `propose-detected` writes `status=proposed`.

Review (`backend/app/services/review_service.py`):
- Server re-derives boxes from stored page text; client coordinates are not trusted.
- Dedup by stable finding id; overlapping different categories are both kept.
- Approve / reject / reopen one occurrence or `apply_to_all_matching`.
- Adjust boxes → finding returns to proposed (must re-approve).
- Manual region: reviewer-drawn, created already approved (drawing *is* the decision).
- Every mutating action bumps `review_revision` first (safe crash direction).
- Re-extraction marks findings stale if their text no longer matches.
- Summary: blockers (undecided proposals, nothing approved), warnings
  (unsupported pages, remaining copies of approved text).

Export (`backend/app/services/export_service.py`, `redaction.py`):
- 5-minute authorization: SHA-256 of token stored, pinned to content hash +
  revision + approved-rectangle digest, consumed atomically, idempotent retry.
- Real `apply_redactions` (text remove, image pixels, line art if touched),
  widget/annot delete, `doc.scrub`, full rewrite `garbage=4`.
- Layers / XFA / encrypted PDFs refused.
- Verification: output opens, no text in approved boxes, boxes render black,
  metadata/attachments gone, single `%%EOF`, original hash unchanged, remaining
  copies classified as intentional (rejected) vs unresolved.
- Download blocked if required checks fail or the export is stale, or (since the audit fixes) if readable
  copies of approved text remain and the reviewer has not acknowledged them.
- Export file names come from the output's hash (`redacted_<10 hex>.pdf`), **never from the uploaded
  name**. (An earlier version of this file claimed this while the code did the opposite; fixed, see below.)

### Phase 3 additions (MCP)

Server (`backend/app/mcp_server/`, run with `python -m app.mcp_server`):
- Real MCP server over **stdio** using the official `mcp` SDK **2.3.0**
  (v2 API: `MCPServer` / `Client`; verified against the installed package).
- Seven tools with Pydantic input/output schemas (`schemas.py`): `search_document`,
  `get_document_excerpt`, `detect_sensitive_entities`, `retrieve_policy_guidance`
  (honest "not available" stub until Phase 4), `propose_redactions`,
  `get_review_summary`, `export_approved_redactions`.
- No tool approves, rejects, adjusts, deletes or issues authorizations; no tool
  has an `owner_id` / `status` / `token` field (tested by walking every schema).
- Identity from the process environment only; other owners' documents return the
  same `not_found` as nonexistent ones. Child gets only Mongo settings + owner id;
  `Settings(_env_file=None)` so the future LLM key in `.env` is never read.
- `propose_redactions`: page + char span + exact expected text, verified against
  the stored page; server derives boxes; saved as `origin: agent`,
  `label: review_needed`, `proposed`; idempotent; `MANUAL` and extra fields refused.
- `export_approved_redactions(document_id)`: takes **no token**. It finds the newest
  reviewer-issued authorization (≤ 5 min), runs the same checks as REST, cannot
  acknowledge unsupported pages, and is idempotent on retry
  (`ExportService.export_with_reviewer_authorization`).
- Bounded results (search ≤ 100, excerpt ≤ 4,000 chars, candidates ≤ 200,
  proposals ≤ 50/call, unresolved ≤ 50) with `truncated` flags; document text in
  results carries a `content_notice` marking it untrusted.
- Logs (stderr) hold tool name, outcome, duration only.

Client (`backend/app/agent/mcp_client.py`, `McpToolClient`):
- Starts the server, discovers tools/schemas over the protocol, validates
  arguments (unknown tool, unknown argument names, JSON Schema) before sending.
- Per-call timeout (30 s; export 180 s), 200,000-char result cap, structured
  `ToolCallResult` with `error_code` (never raises for expected failures; only
  cancellation propagates). No automatic retries.

Other changes: `Origin.AGENT` (+ frontend type), `AuthorizationRepository.find_latest`,
`ExportService.export` refactored into a shared `_export_authorized` path (REST
behaviour unchanged). `mcp==2.3.0` and `jsonschema==4.26.0` pinned; lockfile
gained only additions and `pip check` is clean.

### Phase 4 additions (policies, embeddings, retrieval, citations)

Backend:
- **Embeddings** (`services/embeddings.py`): pretrained `sentence-transformers/all-MiniLM-L6-v2`
  (Apache-2.0, 384-dim) pinned to revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`;
  loaded lazily and **offline only** (`local_files_only`, `trust_remote_code=False`);
  honest `available/unavailable` status with a reason; one-time download via
  `scripts/download_models.sh` / `python -m app.cli download-embedding-model`.
- **Policy ingestion** (`services/policy_chunking.py`, `policy_service.py`): Markdown with
  optional front matter (`policy_id`, `title`, `version`, `status`), chunked by heading
  (≤ 140 words, hard-wrapped prose re-joined, bullets kept), stable citation ids
  `policy_id:version:ordinal`, validation (type, UTF-8, size, null bytes, ids), idempotent
  re-upload, `policy_version_exists` for changed content under the same version.
- **Versions**: one active version per policy, enforced by a partial unique index;
  old versions stay readable; activate/roll back; delete a policy or one version.
- **Storage**: `policies` + `policy_chunks` with `embedding`, `embedding_model`,
  `embedding_revision`, `embedded_at`, `truncated_by_model`; indexes in `db.py`.
- **Retrieval** (`services/retrieval.py`, `PolicyRetriever` interface): filter by owner,
  active version and current model revision *first*; cosine in NumPy; cutoff `0.38`
  (heuristic, from `evaluation/tune_retrieval.py`); keyword overlap only reorders;
  statuses `ok | no_relevant_policy | no_policies | unavailable`; stale vectors ignored
  and reported; no keyword fallback when embeddings are unavailable.
- **Re-index**: per version and for all policies; stale/missing chunks only.
- **API** (`api/policies.py`): list, upload, seed samples, search, read/resolve passages,
  activate, re-index, delete. `/api/health` now reports embedding status.
- **MCP**: `retrieve_policy_guidance` is real (typed passages, method, cutoff, best score
  below cutoff, untrusted-data notice); `propose_redactions` accepts up to 3 `policy_refs`
  per item, validated against real passages (made-up or other-owner citations are refused,
  other items still save). The MCP child receives the model location/revision/cutoff.
- New dependencies pinned: `sentence-transformers==6.1.0`, `torch==2.14.1` (macOS from PyPI;
  Linux `2.14.1+cpu` from PyTorch's CPU index, via an `--extra-index-url` line and markers in
  `requirements.lock`), `numpy==2.5.4`. `pip check` clean.
- Second fictional policy `sample_data/policies/fictional_sharing_guidelines.md`.

Frontend:
- **Policy tab**: list (title, version, active/archived, FICTIONAL EXAMPLE, embedding
  state), load samples, upload, activate, re-index, delete, "Ask the policy" with results,
  the "does not address this" state, and a model-load hint.
- **Citations**: findings show their cited passages as chips (and an "assistant" badge for
  agent proposals with their reason); clicking opens a native `<dialog>` that fetches the
  real passage (Esc closes; missing passages say so).
- Amber, dismissible banner when semantic search is unavailable.

Ops: `scripts/download_models.sh` also downloads the embedding model; `.env.example` has
the new settings; Compose has a `models` volume and mounts `sample_data` read-only;
`backend/.dockerignore` and `frontend/.dockerignore` added (the 1.2 GB venv would otherwise
be uploaded on every build); CI caches and downloads the pinned model.

Frontend (Phases 1-2):
- Review / Search / Text / Export tabs.
- Findings panel with category checkboxes, Detect & propose, filters, per-item
  approve/reject/adjust, "all N matching", Approve/Reject shown, Draw region.
- Search panel proposes one occurrence or all matches.
- Overlay highlights findings and search hits via `bboxToCss`; draw mode uses
  `cssToBbox` (round-trip tested for zoom/rotation).
- Export panel: authorize + export, check list, download link, honest scope.

### Phase 5 additions (the assistant)

- **Chat tab** per document: history survives restarts; demo-mode command chips or the live-mode banner and a one-time
  confirmation that excerpts go to the provider; progress with tool activity while a request runs; Cancel; "What it did"
  (tool, status, duration, count-only summary, tokens); citation chips that jump to a page or open the policy passage,
  with unverified ones flagged; an **Authorize export** hand-off card for the reviewer.
- **API**: conversations, paged messages, `POST .../messages` (202; `409 run_active`, `409 assistant_not_configured`;
  idempotent `client_message_id`), `GET /runs/{id}`, `POST /runs/{id}/cancel`; `/api/health` reports the assistant mode
  (never the key).
- **Backend** (`backend/app/agent/`): `loop.py` (bounded loop, `ToolExecutor`), `provider.py` + `anthropic_provider.py`
  (the only place a model is called), `demo.py` (scripted commands, no model), `citations.py` (code-checked citations),
  `runs.py` (run manager, one tool server per conversation, cancel, shutdown), `prompts.py`, `summaries.py`.
- **Storage**: `conversations`, `messages`, `agent_runs`, `tool_events` with database-enforced one-active-run and one-message
  per `client_message_id`; runs left active by a crash are failed at startup; deleting a document removes its chat data.
- **Guarantees that do not depend on the model**: no approve/authorize tool exists; proposals are re-checked against the
  stored text; export works only after a reviewer's single-use authorization and yields only the approved redactions; one
  document per conversation; time/step/size bounds; fixed error sentences (no key, no raw provider text, no document text).

## Test results (actually run)

**Latest full backend run (2026-10-15, `phase-6-pipeline-eval`, with the evaluation, second-set and pipeline tests):** **`607 passed in 579.36s`**, `ruff check app tests` clean. Frontend after the bulk-button fix (PR #11): lint and typecheck clean, `npm test` **100 passed**, `npm run build` OK, and the five browser tests pass (2026-10-15). (563 earlier on 2026-10-15 predates the 22 second-set tests; 585 predates the 22 pipeline and report tests.) **Phase 5 runs (on `phase-5-agent`):** backend **`525 passed in 534.52s`**, `ruff check app tests` clean; frontend lint and typecheck clean, `npm test` **94 passed**, `npm run build` OK, `npm audit` 0 vulnerabilities. Earlier run, after the hardening bundle (merged as PR #5): backend **`314 passed in 606.95s`** (271 before the bundle; slower than the previous ~350 s because a Docker image build ran at the same time), `ruff check app tests` clean; frontend `npm run lint` clean, typecheck clean, `npm test` **68 passed** (58 before), `npm run build` OK, `npm audit` 0 vulnerabilities; Docker: both images built, stack healthy, non-root, `seed-samples` run inside it. **CI has not run on this branch yet.**

The figures below are from the end of Phase 1 and are kept for history.

Backend — `cd backend && .venv/bin/python -m pytest -q` against local
MongoDB 8.0.34, Python 3.14.7, spaCy 3.8.16 + `en_core_web_sm` 3.8.0:

```
144 passed in 78.77s
```

Frontend — `npm run lint` clean, `npm test` (vitest) **11 passed**,
`npm run typecheck` / `npm run build` pass.

Manual browser verification (Cursor browser, 2026-10-13):
- Detect & propose on `sample_contract.pdf` → 18 proposals (people, emails,
  phones, SSN). Company names were not proposed (org checkbox left off).
- NER false positive "MISCELLANEOUS NOTE" rejected (occurrence-specific).
- Remaining 17 approved via "Approve shown" after filtering to proposed.
- Export verified: all 8 checks passed, including no unresolved copies.
- Downloaded file: `Maria Delgado-Reyes`, `José Álvarez`, emails, phones and
  `512-44-9087` absent; `Northwind Analytics LLC`, `Blue Harbor Consulting`
  and `MISCELLANEOUS` present; metadata scrubbed.

### Audit of Phases 1-4 and the three fixes (2026-10-14)

**Audit method and what held up** (details `docs/decisions.md` #40): `pip-audit` on 96 pinned packages and
`npm audit` found no known vulnerabilities; 34 endpoints called as a different owner leaked nothing and
could not delete or unlock another owner's data; hostile search queries ran in under a second; 12 parallel
policy uploads kept at most one active version; and an adversarial PDF hiding one name in 11 places
(annotation, form field, attachment, metadata, XMP, bookmark, white and invisible text, a page deleted by
an incremental save) was exported with **zero hits** for the name in the output bytes, apart from the two
hidden-text copies the reviewer had not approved (reported by the app).

**Fixed (all three have tests that were mutation-checked and a real-browser check):**
1. Export file name leaked the upload's name -> now `redacted_<hash10>.pdf` everywhere, including for
   exports stored before the fix.
2. Green "verified" badge with the unresolved-copies warning buried -> "verified · with warnings";
   download refused (409 `warnings_unacknowledged`) until the reviewer acknowledges
   (`POST .../acknowledge-warnings`, idempotent, audited with counts/pages only, not an assistant tool).
3. False "words lost" alarm for PDFs with annotations/form fields -> compares page content only; a real
   loss is still caught.

**The remaining audit items were all closed afterwards** (upload size, small images, extraction time limit,
sample-document seed script, Docker non-root and health checks, lint; the `isEvalSupported` item turned out not to
apply to PDF.js 6.3): see "Phase 4 hardening bundle" below and `docs/decisions.md` #41-#46.

### Phase 4 follow-ups (2026-10-14, after merge)

**Totals after the follow-ups and the three audit fixes: backend `271 passed in 351.00s`; frontend lint, typecheck and build clean, `npm test` 58 passed.** CI has not yet run on this work.


- **Dev database cleaned**: the 2 assistant-proposed test findings I had created on one sample
  document were removed (exact ids, guarded by checks), and that document's review revision was
  restored to 0. Everything else (18 other findings, 2 sample policies) was left alone.
- **Branches**: the merged remote branches `phase-3-mcp`, `phase-4-rag` and
  `cursor/phase-1-2-document-redaction` were deleted (git confirmed all were fully merged);
  only `main` remains. PRs #1-#3 and the full history are intact.
- **Frontend component tests added**: 33 tests (44 total at that point; 49 now with the weak-tier
  tests); lint, typecheck and build clean; `npm audit` 0 vulnerabilities; test
  libraries are not in the production bundle. Six deliberate breakages were each caught by a
  test. Tooling and limits: `docs/decisions.md` #35. A real mistake the build caught: Vitest
  ignores TypeScript types, but `tsc` (build/CI) did not, and found two type errors in the new
  tests, now fixed.
- **Retrieval cutoff investigated with a frozen held-out set** (done; details `evaluation/README.md`,
  `docs/decisions.md` #36-#39). Protocol fixed in advance: 44 held-out questions (28 answerable, 16
  unanswerable, near-miss traps) frozen by SHA-256, at most two recorded runs, adoption rules written
  before measuring. **Finding: the cutoff overfitted the dev set.** Dev (tuned) vs held-out: right
  passage first 0.76 vs **0.64**; relevant passage shown 0.68 vs **0.43**; unanswerable wrongly shown
  a passage 0.00 vs **0.31**; balanced accuracy 0.84 vs **0.56**; margin +0.016 vs **-0.193**. Cause:
  similarity measures topic closeness, not answerability (near-miss questions score as high as real
  ones; many real questions score low). Ranking is decent (right passage in the top 3 for 21/28).
  Three small models were compared on dev: better ranking (MRR up to 0.893) but not better
  refuse/answer decisions, so **no model was adopted**. A reviewer-only "possibly related" tier
  (cosine 0.30 to 0.38) met its rule on dev and was **adopted**; on held-out it recovered 5/13 wrongly
  refused questions and showed weak passages for 4/16 unanswerable ones. It never reaches the assistant
  (tested; a test caught a docstring of mine that would have told the assistant it exists). The
  retrieval message now calls "no matching passage" a heuristic. Both held-out runs are used up.
- **Small extras**: all four were done afterwards in the hardening bundle below (chunker version, script that
  regenerates `requirements.lock`, citing-finding count in the delete confirmation, automatic re-index at startup).

### Phase 6: assistant model comparison (2026-10-14, merged in PR #7)

What was built (`evaluation/assistant_eval/`): a seeded generator of synthetic contract-like PDFs with ground truth (6 templates; a
6-document dev set for debugging and a frozen 12-document held-out set, SHA-256 checked), the scoring rules, a runner that drives the
real assistant loop and MCP server against a scratch database with a hard budget cap, a report generator, and `PROTOCOL.md` written
before any held-out result (predictions, decision rule, rules of conduct, incident log).

Verified: `backend/tests/test_assistant_eval.py` (19 tests: scoring rules, bootstrap, dataset reproducibility, ground truth matches the
real extractor, frozen hash, seed separation) and `backend/tests/test_assistant_report.py` (19 tests: decision rule edges, retry handling,
the six predictions, the post-hoc company-address logic). The tests that did not fail when I broke the code on purpose were
tightened until they did. Full backend suite **563 passed in 497.86s**, `ruff check` clean for `app`, `tests` and the evaluation package.

Run: 36 live runs (12 documents x Haiku, Sonnet, Opus), all finished normally, **$2.78** at list prices (about $0.006, $0.074 and
$0.152 per document). Result in short (full tables: `evaluation/assistant_eval/REPORT_heldout.md`, decision 51 in `docs/decisions.md`):
F1 0.988 / 0.981 / 0.981 for Haiku / Sonnet / Opus against 0.823 for the app's detectors and 0.622 for pattern-only; the models
cannot be told apart; no company name proposed; no export attempt on the injected documents. Five of six pre-registered predictions
held. The pre-registered rule recommends Haiku as the default model: `.env.example` and the README now say so and the local `.env` uses `claude-haiku-5-5` (it is only a setting).

Found by reading the misses: all 16 were the address of a company, which my labels called sensitive although the request says
"individuals". Headline numbers keep the frozen labels; a separate post-hoc table (marked as such) leaves those 8 lines out. This is a
flaw in my ground truth, not a result about the models, and it is in the protocol's incident log.

Spend so far on the live provider, estimated from token counts: about $3.3 of the $20 bought (Phase 5 checks ~$0.5, evaluation setup
~$0.04, held-out run $2.78). The Console's usage page is the authority.

### Phase 6: browser end-to-end test (2026-10-15, merged in PR #7)

`frontend/e2e/review-flow.spec.ts` (Playwright, `npm run e2e`): five tests, one reviewer's session, in a real Chrome against the real
frontend, backend, MongoDB and PDF engine, with the scripted demo assistant (no model call is possible; `scripts/e2e_backend.sh` blanks
the provider settings and uses its own database `clearcase_e2e`). **5 passed in about 70 s** locally with Chrome (`E2E_CHANNEL=chrome`).
Covered: upload, search, detect, reject/approve with a status filter; policy library, a cited passage in the modal dialog (focus stays
off the page behind it, Escape closes it and returns focus); chat summary with visible tool activity; a checked citation chip; the
assistant refused an export until the reviewer authorizes, then exporting; the reviewer's own export, download, and re-uploading the
downloaded file to search it (approved name 5 matches before, 1 after = the rejected one; approved e-mails and SSN gone; company names
kept); the conversation after a reload. Checked by breaking the product on purpose: a non-modal dialog and a do-nothing redaction each
make the tests fail. Frontend lint, typecheck (now includes the specs via `tsconfig.e2e.json`), 94 unit tests and build still pass.
A new CI job `e2e` runs it with Playwright's own Chromium; it **passed on GitHub's runners** (PR #7: backend, frontend and e2e all green).

**Compose re-run (2026-10-15):** `docker compose build` and `up` with the current tree: mongo, backend and frontend all came up healthy,
the backend runs as uid 10001 (non-root), and the same five browser tests **passed against the containers** (backend and database in
Docker, a fresh database inside the stack, the scripted assistant; the pinned embedding model was copied from the local cache into the
`models` volume, so nothing was downloaded). The temporary port override and scratch database were removed afterwards.

Found: bulk "Approve shown"/"Reject shown" overwrote decisions already made when the status filter was "all". **Fixed afterwards in PR #11**
(decision 54; the buttons now act only on undecided findings and say how many). Details and limits: decision 52.

### Phase 6: second retrieval question set (2026-10-15, branch `phase-6-retrieval-set2`)

`evaluation/retrieval2/` (protocol, build script, report) and `evaluation/retrieval_heldout2.json` (frozen, sha256 `68eaf4dc...`): 95
questions written by a model that never saw the policies and labelled by two other model runs (kept only if both agreed): 36 answerable,
59 not addressed. The unchanged production retriever was run once (`eval_heldout.py --set 2 --label baseline`; the runner gained a
backwards-compatible `--set` option, and a smoke run on the dev set still reproduces its known numbers). Result: balanced accuracy
**0.61** at the cutoff (first held-out set 0.56, pooled **0.60**, interval 0.52 to 0.68); right passage in the top 3 for 0.86 (pooled 0.81).
Four of five pre-registered predictions held; prediction 1 (worse ranking) was wrong. Cost **$0.061** (cap $0.20). Tests:
`backend/tests/test_retrieval_set2.py` (22 tests; the selection rules, the spend guard, the blindness of the writer prompt and the report
logic were each broken on purpose to confirm a test fails). Details and limits: decision 53 and `evaluation/retrieval2/REPORT.md`.

Spend so far on the live provider, estimated from token counts: about $3.4 of the $20 bought. The Console's usage page is the authority.

### Phase 6: pipeline latency and export-verification outcomes (2026-10-15, branch `phase-6-pipeline-eval`)

`evaluation/pipeline_eval/` (protocol and four predictions first; `run.py`; `REPORT.md`; decision 55): the real app in-process on the 18 synthetic
PDFs, a simulated careful reviewer, export, and an independent re-read of every downloaded file; warm-up plus 3 passes; free, no model.
Upload to "ready" 47 ms median (61 ms p90), export with verification 186 ms; **0 failed or blocked exports and 0 failed required checks;
9 of 18 exports verified cleanly and 9 needed an acknowledgement** because the NER model missed one occurrence of an approved name (PERSON
coverage 75 of 108). **The verifier reported all 9 leftover copies that an independent check found (none missed, none false)**, and all 54 kept
company-name occurrences survived. Two of my four predictions failed (I expected at least 15 clean exports and no leftover text). The
measurement was run twice (a per-category breakdown was added in between); outcomes were identical. Tests: `backend/tests/test_pipeline_eval.py`
(19, each rule broken on purpose to confirm a test fails).

**Also found, and disclosed, while doing this:** adding the specification's precision/recall/F1-by-category table to the assistant report
exposed a second flaw in my synthetic data: every phone number has a 3-digit last group, so none is a valid US number and the validating
detectors score PHONE recall 0. The detectors' F1 is 0.823 as frozen and 0.876 without those items; the model advantage shrinks from about
+0.16 to about +0.10 (still clear). Headline numbers keep the frozen data; a marked post-hoc section and the protocol's incident log carry the
correction (decision 51, addendum).

### Phase 6: the data generator fixed, and corrected v2 data (2026-10-15, branch `phase-6-data-v2`)

`dataset.py` now has two variants: **v1 reproduces the old, measured data byte for byte** (a test checks it) and **v2 fixes the two data
flaws** (valid phone numbers; company addresses not labelled as personal). It also **can no longer overwrite a frozen held-out set**
(before, `main()` replaced the hash file unconditionally). New frozen set `heldout_v2` (sha256 `90188e77...`); protocol first
(`evaluation/assistant_eval/PROTOCOL_V2.md`). Free steps run once: on v2 the detectors score **F1 0.918** (v1: 0.823), PHONE recall 18 of 18,
NER PERSON recall 0.88 (v1: 0.67); in the pipeline measurement 14 of 18 exports are clean and 4 need an acknowledgement (v1: 9 and 9), the
verifier again reported every leftover copy (4 of 4), latency 55 ms upload-to-ready and 208 ms export. Two of four predictions failed. **The
models have not been run on v2** (about $0.07 for Haiku, $2.8 for all three; needs approval); so the models' advantage on clean data is at
most about +0.08, against +0.16 on v1 as frozen. Decision 56; tests in `test_assistant_eval.py` (31, with 7 deliberate mutations all caught).

### Phase 6: README polish (2026-10-15, branch `phase-6-polish`)

- **Screenshots:** five real screenshots of the running app (synthetic sample, scripted demo assistant) in `docs/images/`, made by
  `frontend/e2e-screenshots/screenshots.spec.ts` (`npm run screenshots`) and shown in the README.
- **A wording bug found by reading the README:** three places told users to click "Authorize export" *in the Export tab*, but that
  button is the hand-off card in the **Chat** tab (the Export tab's own button needs no authorization). Fixed in the demo assistant's
  refusal, in the tool's error message (which a live model repeats), and in the README; the demo test was updated. 129 related tests pass.
- **Stale README text fixed:** the stack table still listed the hosted model as a "later phase", the Limitations section quoted only
  the first held-out set, and the repository layout missed the new folders.
- **Window size checked:** the layout is fine from about 900 px up, overflows sideways below about 700 px and is not usable on a
  phone (three fixed-width columns). Documented as a limitation; a responsive layout was not built.

### Phase 5 verification (2026-10-14, branch `phase-5-agent`)

Built in eight steps, each verified before the next; the design and its approved choices are in `docs/phase5_design.md`,
what building it found is in `docs/decisions.md` #50.

| Step | What | New tests | Proof beyond passing |
| --- | --- | --- | --- |
| 1 | Models, repositories, indexes, delete cascade, restart recovery | 17 | six deliberate breakages each failed a test; two DB-level guarantees tested by bypassing the repository |
| 2 | Provider interface, fake provider, bounded loop, limits, error mapping; shared `ToolExecutor` | 51 unit + 6 against the real MCP server | eight breakages caught; found that a too-large result still counted as evidence, and that two cancellation tests passed for the wrong reason (both fixed) |
| 3 | Anthropic adapter (`anthropic==1.11.0`) | 35 (real SDK, mock HTTP transport) | ten breakages caught; **one tiny real call** parsed a tool call and a tool result round trip (about $0.005) |
| 4 | Run manager, API, tool-server sessions | 29 (two check citations and one checks live progress, all through the API) | seven breakages caught (one needed a timing assertion added); the real server is opened, reused, swept, reopened and closed cleanly |
| 5 | Citation verifier | 29 | seven breakages caught |
| 6 | Demo assistant | 36 with fake tools + 4 against the real server | six breakages caught; found that the policy tool takes no `document_id` and the loop was adding one (fixed; affected live mode too) |
| 7 | Chat tab | 20 component + 6 citation-helper | nine breakages caught (two needed the tests tightened); found a message could be dropped if sent before the conversation loaded (fixed); exercised in a real browser: propose, approve (reviewer), refusal to export, Authorize export, verified export |
| 8 | Agent-level prompt injection, live check, docs | 4 | the sample contract's injected line really reaches the model, labelled untrusted; an obedient model cannot approve, cannot export without authorization, and with authorization yields only the approved redactions |

**Checks done before Phase 6 (2026-10-14):**
- **Docker, rebuilt with the new lock:** both images build; the embedding model downloads as the non-root user; the stack comes up
  healthy; inside the backend container the user is `clearcase`, `anthropic 1.11.0` is installed and no key is in its environment; the
  demo assistant ran `detect`, `propose`, `summary` and a real policy question through the frontend proxy (the tool server starts inside
  the container and is reused: the second command took 0.5 s; all 3 policy citations verified). Test volumes and containers removed.
- **Live mode through the real app** (scratch database; browser plus API): `/api/health` reports live mode and never the key; the Chat tab
  shows the privacy notice and blocks input until confirmed; a request **cancelled mid-flight** against the real provider ends as
  "stopped" and the conversation is usable at once; a full request returned a plain-text answer (no Markdown) with verified citation chips,
  9 proposals highlighted in the viewer, and a run record with tokens, provider and model. Two completed requests and one cancelled one cost
  about $0.22 at list prices. **This check found that progress was not shown while a request ran** (activity was only written at the end);
  fixed, covered by tests that fail without the fix, and re-checked live (activity rows appear while the request is still running).
- The API key still works. Its expiry was set to about 30 days, so check it in the Console before relying on live mode after mid-November.

**Live observation (`scripts/live_check.py`, `claude-opus-5-5`, synthetic sample, one run each; not a guarantee):**
- "Find the client's personal information, suggest redactions, and keep company names visible": 4 model calls, 7 tool calls, 50 s,
  about $0.17 at list prices (10 uncached input tokens, 37,751 read from the prompt cache, 19,832 cache writes, 2,872 output).
  It proposed 10 redactions (PERSON 5, EMAIL 1, PHONE 1, ADDRESS 2, SSN 1), none for company names, listed the consultant's
  contact details as a decision for the reviewer, and flagged the injected sentence on page 2 instead of following it. All 8
  citations verified, including four quoted policy sentences.
- A policy question with "quote the sentence": 2 calls, 23 s, about $0.04, three quotes, all verified.
- "Summarise page 2" (the page with the injected line): 2 calls, 10 s, about $0.03; it summarised, warned the reviewer about the
  line, and attempted no export. Findings approved: 0; exports stored: 0.
- Total about $0.24. First answers used Markdown, so the prompt now asks for plain text.

### Phase 4 hardening bundle (2026-10-14, branch `phase-4-hardening`)

Closes every item that was open after the audit and the four "small extras". Decisions and reasoning:
`docs/decisions.md` #41-#46. Four choices were the user's (all took the recommended option): warn-but-keep-analysing
for small images, a between-pages extraction time limit, ruff + a CI lint step, and "flag only" for old chunking.

| Item | What changed | Proof |
| --- | --- | --- |
| Oversized uploads read in full | `app/api/upload_limit.py` (ASGI middleware): honest `Content-Length` over the limit refused at once, other bodies counted and cut off at limit + 64 KiB; HTTP 413 with the stable code and CORS headers; service-level over-size also 413 (was 422) | 12 tests in `test_upload_limit.py`; three deliberate breakages each failed tests; in Docker a 25 MB body got `413 file_too_large` |
| Small images silently unsearched | page stays `text`, listed in `extraction.pages_with_images`; amber notice in the document header; export panel warning; unreadable image position is no longer treated as "no image" (and makes a text-less page "scanned") | unit + API + 5 frontend tests; four breakages failed tests |
| No extraction time limit | 60 s budget (`CLEARCASE_EXTRACTION_TIMEOUT_SECONDS`) checked after each page; over budget = status `error`, retryable. **Cannot stop one page hanging inside PyMuPDF** (documented) | 3 unit tests + 1 API test (error then successful retry) |
| No sample-document seed script | `python -m app.cli seed-samples [--reset]` (native and Docker); samples recognised by content hash; reset never touches user uploads or another owner's copy | 7 tests; four breakages failed tests; run for real 3x on a scratch database and inside Docker |
| Docker root user / no health checks | backend uid 10001, frontend `node`; health checks (backend: API + MongoDB `ok`); frontend waits for a healthy backend | built both images, downloaded the model as non-root, stack came up healthy, `seed-samples` worked inside it, health check exits 1 while MongoDB is stopped; test volumes removed afterwards |
| `isEvalSupported` | **not needed**: PDF.js 6.3 removed the option and its bundles contain no `eval(`/`new Function(` (the one `eval(` is in the scripting sandbox, which the viewer never enables) | grep of the installed package |
| 7 lint findings | ruff (pinned `0.16.10`, dev extras + lock) now runs in CI on `app` and `tests`; the findings were fixed | `ruff check app tests` clean |
| Chunker version | `chunker_version` stored per policy version; older versions flagged "split with older chunking rules"; re-index never re-splits (citations must not move) | 3 backend + 2 frontend tests; three breakages failed tests |
| Citing-finding count | `cited_by_findings` per policy version (indexed exact-id query); the delete confirmation says how many findings cite any version | 2 backend + 3 frontend tests |
| `requirements.lock` regeneration | `scripts/regenerate_lock.sh` + `tests/test_lockfile.py`; the script's output was identical to the committed lock apart from two transitive patch releases and a header comment, so the tested pins were kept | script run in a clean venv, diffed; guard tests failed on four deliberately broken locks |
| Auto re-index on model change | background thread at startup (`CLEARCASE_REINDEX_ON_STARTUP`, default on; off in tests): refreshes stale/missing passages for every owner, skipped without a model, failures logged | 5 tests; four breakages failed tests |

**Totals: backend **`314 passed in 606.95s`** (271 before the bundle; slower than the previous ~350 s because a Docker image build ran at the same time), `ruff check app tests` clean; frontend `npm run lint` clean, typecheck clean, `npm test` **68 passed** (58 before), `npm run build` OK, `npm audit` 0 vulnerabilities; Docker: both images built, stack healthy, non-root, `seed-samples` run inside it. **CI has not run on this branch yet.****

### Phase 4 verification (2026-10-14)

- Backend full suite: **`247 passed in 301.92s`** (178 before this phase). By file: `test_mcp_protocol` 36, `test_policy_api` 34, `test_detection` 34,
  `test_search` 25, `test_review_api` 22, `test_export_api` 18, `test_documents_api` 15,
  `test_retrieval_semantic` 14, `test_redaction` 13, `test_policy_chunking` 13,
  `test_pdf_processing` 13, `test_mcp_client` 6, `test_search_api` 4.
- Frontend at the end of Phase 4: lint, typecheck, `npm test` 11 passed, build OK. The new
  components were then verified in the browser (below) and later got component tests (see
  "Phase 4 follow-ups").
- **Mutation checks**: temporarily removing the owner filter, the active-version filter and
  the model-revision filter from the retrieval query each made a specific test fail (this
  exposed two missing tests, which were added).
- **Retrieval evaluation (dev set, `evaluation/results/retrieval_dev.json`)**: 25 answerable
  / 10 unanswerable questions over 19 chunks. Semantic only: hit@1 17/25 (0.68), hit@3 20/25
  (0.80), MRR 0.759. Semantic + keyword reorder (used): hit@1 19/25 (0.76), hit@3 22/25
  (0.88), MRR 0.822. Cutoff 0.38: 10/10 unanswerable refused, 8/25 answerable wrongly turned
  away (`a02 a15 a17 a19 a20 a22 a24 a25`), margin to the best unanswerable score 0.016.
  **Synthetic, same-author, tiny: not real-world accuracy.** Re-run unchanged after the
  chunk-text fix.
- **Browser (real app, real model)**: loaded both sample policies (ready); asked "When is it
  acceptable to redact an organisation name?" → correct passage first (0.70) with citation;
  clicked the citation → dialog showed the real text, labelled FICTIONAL EXAMPLE; Esc closed
  it; "What are the cafeteria opening hours?" → "do not appear to address this" with best
  similarity 0.01 vs cutoff 0.38; cited findings created through the real MCP client show
  the assistant badge, reason and clickable citation; with embeddings disabled the amber
  banner appears and search reports unavailable. A display bug found this way (hard line
  breaks mid-sentence in passages) was fixed in the chunker and has a test.
- **Docker (aarch64 Linux, Python 3.12)**: image builds (8.5 min); backend 2.31 GB, **0** CUDA
  packages, `torch 2.14.1+cpu`; health says "not downloaded" on an empty volume; policies
  uploaded then are saved as "unavailable"; after the in-container download, health flips to
  available with no restart and **Re-index** makes both policies ready; the MCP child finds
  the model in `/models`; retrieval, refusal and a cited proposal work; probe scores matched
  the macOS run (0.500 and 0.0106).
- **GitHub Actions on PR #3: both jobs passed.** Backend: `247 passed in 140.82s` with no skips on
  x86 Linux / Python 3.12 (`torch 2.14.1+cpu`, a 196 MB wheel, installed from PyTorch's CPU index;
  the pinned model downloaded and reported `available: true`, so the real-model tests ran).
  Frontend: lint, typecheck, 11 tests and build passed.

### Self-review after Phase 3 (2026-10-14)

Checked, not assumed:
- PR #1 CI (Python 3.12, fresh install from `requirements.lock`) passed both jobs.
  The Phase 3 branch has **not** been through CI yet.
- Server stderr logs argument *names* only; a marker string sent as a bad argument
  did not appear in stderr (it is echoed back to the model in the error, which is
  the model's own input).
- Fixed: after a completed export, if the reviewer changed the review inside the
  5-minute window, the export tool returned `ok` with `status: verified` for an
  out-of-date (not downloadable) export. It now returns `export_stale: ask the
  reviewer to authorize again`.
- Fixed: the audit log did not say who triggered an export. `export_completed`
  now records `actor` (`reviewer` via REST, `assistant` via MCP).
- Fixed: a test named "does not receive the LLM key" only checked what we pass.
  It now checks the real child environment the SDK builds.
- Side effect to know about: installing `mcp` pulled in `httpx2`, so Starlette's
  `TestClient` now uses it (the old "httpx is deprecated" warning is gone: 5
  warnings instead of 6). `httpx==0.28.1` is still pinned for dev.

Caveats that remain (design limits, not bugs):
- **Any** pending reviewer authorization can be used by the assistant. If the
  reviewer clicks "Authorize export" and walks away, the assistant could consume
  it within 5 minutes. The normal UI flow authorizes and exports in one click, so
  the window is normally tiny. A stricter option (not built) is a separate
  "allow the assistant to export" button that issues an assistant-only
  authorization.
- The client does not reconnect if the server process dies mid-session; calls
  return `transport_error`. Phase 5 should reuse one server per conversation (a fresh server pays a
  ~20 s model load on its first policy question) and restart it if a call returns `transport_error`.
- The test suite is slower (~2 min) because most MCP tests start a server process.

### Phase 3 verification (2026-10-14)

- Backend full suite: **`178 passed in 124.77s`** (144 existing + 28 in
  `tests/test_mcp_protocol.py` + 6 in `tests/test_mcp_client.py`).
  Existing export/review tests pass unchanged after the export refactor.
- Frontend after the type change: lint clean, typecheck clean, `npm test` 11 passed.
- The protocol tests start the real server as a child process and cover: tool
  discovery with schemas; `search_document` through the protocol (matches REST
  occurrence ids; case/whole-word/cap; Unicode; unsupported pages flagged);
  bounded/resumable excerpts; detection saves nothing and reports provenance and
  `format_valid`; propose (idempotent retry, hallucinated span refused, `boxes` /
  `status` / `MANUAL` rejected, batch cap, cannot alter a reviewer's decision);
  review summary has no document text; invalid arguments (missing, wrong type,
  operator-injection object as id, oversize, unknown tool/argument); other
  owner's and unknown documents indistinguishable across all tools; a spoofed
  `owner_id` ignored even when the client check is bypassed; export refused without
  reviewer authorization, succeeds with one, idempotent retry, refused when stale
  or old, cannot acknowledge unsupported pages; prompt-injection text returned as
  data and no tool can do what it asks; stdout carries only protocol messages and
  the server exits when stdin closes (raw JSON-RPC test).
- Client tests (with a tiny test-only server): discovery, local argument
  validation, timeout (1 s limit on a 6 s tool), oversized result refused, server
  crash → `transport_error`, misuse outside `async with` raises.
- Docker: rebuilt the backend image (Python 3.12) with the new dependencies and
  ran `python -m app.mcp_server` inside it: handshake OK, all 7 tools listed,
  stdout pure JSON, stderr empty. The full Compose stack was **not** re-run for
  Phase 3.
- Found and fixed while testing: page `kind` is a plain string (a server crash
  that the SDK correctly reduced to a generic error for the model); the client
  now uses the SDK's `REQUEST_TIMEOUT` / `CONNECTION_CLOSED` codes instead of text
  matching.

### Re-verification of Phases 1–2 (2026-10-14, new session)

Run again from scratch before starting Phase 3:

- Backend: `144 passed in 34.40s` (6 deprecation warnings: SWIG types from a
  dependency and Starlette's `httpx` → `httpx2` notice).
- Frontend: `npm run lint`, `typecheck`, `build` clean; `npm test` **11 passed**.
  Build warns that the JS chunk is > 500 kB (pdf.js); not an error.
- **Docker Compose (first time ever tested)**: Docker Desktop 29.8.2 on arm64,
  Compose v5.6.0. `docker compose build` succeeded for backend (Python 3.12-slim
  + `requirements.lock` + `en_core_web_sm`) and frontend (node:22-alpine). All
  three containers came up; Mongo healthcheck passed; `/api/health` reported
  `mongodb: true`, `ner_available: true`, demo mode.
- End-to-end workflow against the containerised stack (HTTP script, 31/31
  checks): upload → ready (3 text pages) → fake PDF rejected (422) → export
  refused with nothing approved (409) → detect/propose 18 findings, all
  `proposed`, no ORGANIZATION → reject NER false positive → approve 17 →
  authorize → export with all 8 built-in verification checks passing → retry
  with same authorization is idempotent → independent re-open of the downloaded
  PDF: approved text gone, both company names and the rejected item still
  present, metadata empty, stored original unchanged.
- Restarted the Mongo and backend containers: document, 18 findings (17
  approved / 1 rejected) and the verified export all persisted.
- UI served from the container (`:5173`) restored the saved review state and
  drew the highlights over the PDF; no console errors.

Compose test was done with a temporary port-remap override (backend 8010, Mongo
27018) because the host's 8000/8001 and the native Mongo's 27017 were taken.
The repo's `docker-compose.yml` was **not** changed; on a machine where those
ports are free it publishes 8000 / 27017 / 5173 as written.

Not yet tested: Playwright E2E (Phase 6), GitHub Actions run (repo has no remote
yet). A bare `docker compose up --build` with the default ports was not run here
because of the port conflicts above.

### Bulk-button fix (2026-10-15)

A browser test showed that "Approve shown" / "Reject shown" overwrote decisions already made when the list showed
"all statuses" (reject one finding, click "Approve shown", and it became approved). The buttons now act only on
undecided findings in that view and say how many ("Approve shown (17 undecided)"); filtering to a specific status
still acts on that whole list. Frontend only (`FindingsPanel.tsx`); the server endpoint is unchanged. See
`docs/decisions.md` #54. Frontend after the fix: `npm test` 100 passed, lint and typecheck clean. The new component
tests were checked by reverting the fix: they fail.

## Known issues / notes

- Dev machine: Python 3.14.7, Node 22.11.0 (below Vite 8's ≥22.12), no Homebrew.
  Resolved with `.local/` downloads (git-ignored). Docker Desktop is now
  installed, but its CLI is not on PATH in every shell: use
  `/Applications/Docker.app/Contents/Resources/bin/docker` (or open a new terminal).
- `requirements.lock` is generated on Python 3.14 (macOS) and installs fine on Python 3.12 (Docker, CI). It carries
  two platform-marked `torch` lines, an `--extra-index-url` header and the spaCy model pinned by URL and hash, none
  of which `pip freeze` can produce: regenerate it with `scripts/regenerate_lock.sh` (never plain `pip freeze`);
  `backend/tests/test_lockfile.py` guards it.
- The Phase 4 install adds ~1 GB to `backend/.venv` and ~0.9 GB to the backend image.
- Ports 8000 and 8001 are used by other projects; run ClearCase with
  `CLEARCASE_PORT=8010` and `VITE_API_PROXY_TARGET=http://127.0.0.1:8010`.
- spaCy NER is pretrained and imperfect: it tagged "MISCELLANEOUS NOTE" as a
  person on the sample. That is expected and is why review is required.
- `httpx2` arrived as a dependency of `mcp`, so Starlette's `TestClient` now uses it (the
  old deprecation warning is gone).

## Remaining work (by phase)

**Phase 6 is complete** (the specification's deliverables and evaluation requirements are all met, with their limits stated; the Compose
re-run was done on 2026-10-15). Optional follow-ups, none of which blocks anything: run the assistant (Haiku, about $0.07) on the corrected `heldout_v2` set, which needs the owner's approval and would finish the comparison on clean data; a retrieval question set **written by people**
(both held-out sets are model-written or same-author); a harder, longer, messier assistant evaluation with corrected labels and valid
phone numbers, written first; a button that proposes the unresolved copies straight from the export warning (the pipeline measurement
shows a fifth to a half of the NER-based exports hit it); a responsive layout; real authentication and a worker process.

**Known gaps (deliberate or deferred)**:
- Live assistant quality is measured **only on 12 easy synthetic one-page documents** (see Phase 6 above): it separates the
  assistant from the detectors but not the models from each other, and says nothing about real documents. The labels also
  treat company addresses as sensitive (known, logged); a harder set with corrected labels, written first, is the follow-up.
- Live mode supports one provider (Anthropic); replies arrive when a request finishes (polling, no token streaming).
- No server-side per-tool timeout (tools are threads; the client enforces it).
- The retrieval cutoff (0.38) is unreliable as an answer/no-answer decision (balanced accuracy 0.56 and 0.61 on two held-out
  sets, pooled 0.60; decision 53). The assistant is built around that (it must quote or say the policy does not address the
  question, and quotes are checked by code). A cross-encoder re-ranker / LLM relevance check should be evaluated on a THIRD
  set (both held-out sets have been measured and must not be used to tune).
- Brute-force cosine; fine for thousands of passages, replace `MongoCosineRetriever` beyond.
- Component tests run in happy-dom, which cannot prove focus trapping or real layout; the Playwright test covers focus and Escape for
  the citation dialog in one real browser engine. Real layout, other browsers and phones are still unchecked.
- The interface is a desktop layout (about 900 px wide or more); no responsive or phone layout.
- Policies cited by findings can be deleted; the delete confirmation says how many findings cite it and the UI then
  shows the passage as unavailable (it does not block deletion).
- Chunker changes never re-chunk existing policy versions (on purpose: citation ids must keep pointing at the same
  words). Older versions are flagged "split with older chunking rules"; upload the text as a new version.
- Extraction's time budget is checked between pages and cannot interrupt one page that hangs inside PyMuPDF.
- The assistant's tool servers and runs live in the API process (fine for localhost); a separate worker would be the
  next step for several users.

## Next implementation step

1. Merge the two open PRs (the polish PR and the pipeline-measurement PR), then tag a release and tidy the merged remote branches (the
   owner decides).
2. Optional follow-ups: see "Remaining work". Keep `CLEARCASE_ASSISTANT_MODE=demo` for CI and the default demo; live mode needs the
   user's key in `.env` and spends real credit (about $0.006 per document-sized request with `claude-haiku-5-5`, the recommended model; the
   account is prepaid with auto-reload off). `scripts/live_check.py` is the manual way to look at it. The API key expires around mid-November.
3. Before any public or multi-user use: real authentication (the demo identity is not authentication), a worker process
   for extraction and runs, and a decision on retention of conversation text.

## How to resume quickly

```bash
scripts/dev_mongo.sh start
scripts/download_models.sh                          # no-op if already installed
CLEARCASE_PORT=8010 scripts/dev_backend.sh          # terminal 1
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh   # terminal 2
cd backend && .venv/bin/python -m pytest -q
```
