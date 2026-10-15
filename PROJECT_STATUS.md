# ClearCase — project status

Last updated: 2026-10-14 (Phase 4 complete and verified, plus a hardening bundle that closed every open audit item; Phase 5 not started; hosted provider chosen: Anthropic)

This file is the hand-off point for any new session. Read it first, then
`docs/ClearCase_Cursor_Prompt.md` (the specification) and `docs/decisions.md`.

## Phase progress

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Repository setup, MongoDB, upload, extraction, PDF viewing | **Complete and verified** |
| 2 | Search, candidate detection, manual review, real export, verification | **Complete and verified** |
| 3 | Real MCP server/client integration with tested tools | **Complete and verified** (merged to `main`) |
| 4 | Policy ingestion, embeddings, retrieval, source citations | **Complete and verified** (PR #3, merged to `main`) |
| 5 | Live agent integration, persisted chat, visible tool activity | Not started |
| 6 | Integration tests, evaluation, interface polish, documentation | Not started |

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

## Test results (actually run)

**Latest full runs (2026-10-14, after the hardening bundle):** backend **`314 passed in 606.95s`** (271 before the bundle; slower than the previous ~350 s because a Docker image build ran at the same time), `ruff check app tests` clean; frontend `npm run lint` clean, typecheck clean, `npm test` **68 passed** (58 before), `npm run build` OK, `npm audit` 0 vulnerabilities; Docker: both images built, stack healthy, non-root, `seed-samples` run inside it. **CI has not run on this branch yet.**

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

**Phase 5 — Agent** (read `docs/decisions.md` #39 first): bounded tool loop using `McpToolClient` (reuse ONE MCP server per
conversation: each fresh server pays a ~20 s model-load on its first policy question), one hosted provider
via its official SDK (verify model ids against current docs first), demo mode with
scripted commands, persisted conversations/messages/agent_runs/tool_events,
prompt-injection resistance in the system prompt (the structural part is done),
no chain-of-thought storage, Chat tab, "excerpts are sent to a hosted provider"
notice, and a reviewer-visible **Authorize export** hand-off message.

**Phase 6 — QA & docs**: Playwright E2E (upload → search/analyse → review →
approve → export → reopen conversation), synthetic evaluation, demo script,
interview guide, evaluation report, polish, full Compose re-run.

**Known gaps (deliberate or deferred)**:
- No server-side per-tool timeout (tools are threads; the client enforces it).
- The UI does not call MCP yet (Phase 5).
- Tool results include document text snippets by design; Phase 5 must show the user when
  text goes to a hosted provider.
- The retrieval cutoff (0.38) is unreliable as an answer/no-answer decision (held-out balanced
  accuracy 0.56). Phase 5 must not rely on it alone: give the assistant top candidates with scores and
  require it to quote supporting text or say the policy does not address the question. A cross-encoder
  re-ranker / LLM relevance check should be evaluated on a NEW held-out set (the current one has had
  its two allowed runs).
- Brute-force cosine; fine for thousands of passages, replace `MongoCosineRetriever` beyond.
- Component tests run in happy-dom, which cannot prove Escape-to-close, focus trapping or real
  layout. The Phase 6 Playwright test must cover the Policy tab and the citation dialog in a
  real browser.
- Policies cited by findings can be deleted; the delete confirmation now says how many findings cite it and the UI
  then shows the passage as unavailable (it does not block deletion).
- Chunker changes never re-chunk existing policy versions (on purpose: citation ids must keep pointing at the same
  words). Older versions are flagged "split with older chunking rules"; upload the text as a new version.
- Extraction's time budget is checked between pages and cannot interrupt one page that hangs inside PyMuPDF.

## Next implementation step

Start **Phase 5** (live agent, persisted chat, visible tool activity). **Provider chosen: Anthropic.** The user created
an API key in the Claude Console (personal key, Default workspace, short expiry) and put it in the local, git-ignored
`.env` (`CLEARCASE_LLM_PROVIDER=anthropic`, `CLEARCASE_LLM_MODEL=claude-opus-5-5`; the key itself was never saved
anywhere else, and a first key that was pasted into chat was deleted and replaced). Two tiny live requests (Haiku and
Opus, 12 tokens in / 4 out, well under a cent) confirmed the key, the credit balance and the model id. Keep
`CLEARCASE_ASSISTANT_MODE=demo` until the live path is built. Spend is capped by prepaid credit with auto-reload off.

**Decided by the user on 2026-10-14 (the recommended options; `docs/decisions.md` #47-#49):** the assistant reads the
top passages and must quote the supporting sentence or say the policy does not address the question (the cutoff is not
trusted alone); policy scope stays global (every active version, citations pin the exact version); no manual policy
attach for reviewers. **The Phase 5 design is drafted in `docs/phase5_design.md` and awaits the user's approval (section 15 lists the choices they can veto); do not build before that.**

1. Verify the chosen hosted provider's current **official SDK**, tool-calling API and a
   valid **model id** against its documentation before writing any code (do not guess
   ids; use the `claude-api` skill). Keep the provider adapter separate; read key/model from env.
2. Collections: `conversations`, `messages` (one record per message, paginated),
   `agent_runs`, `tool_events` (tool, status, duration, sanitized summary; never raw
   document text, chain-of-thought or keys). Persist across restarts.
3. Bounded loop (rounds, time, context size) over `McpToolClient`; handle rate limits,
   provider errors, cancellation, malformed arguments; retries must not duplicate
   proposals/exports (proposals are already idempotent).
4. Demo mode: clearly labelled scripted commands with deterministic tool results; live mode
   shows clear setup errors and never silently falls back to simulated output.
5. Chat tab; show tool activity; show a notice when excerpts go to a hosted provider; cite
   real pages and policy citation ids; reviewer-visible "Authorize export" hand-off.
6. Prompt-injection tests at the agent level (the structural protections already exist).

## How to resume quickly

```bash
scripts/dev_mongo.sh start
scripts/download_models.sh                          # no-op if already installed
CLEARCASE_PORT=8010 scripts/dev_backend.sh          # terminal 1
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh   # terminal 2
cd backend && .venv/bin/python -m pytest -q
```
