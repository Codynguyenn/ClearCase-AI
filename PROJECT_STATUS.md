# ClearCase — project status

Last updated: 2026-10-14 (Phase 3 complete and verified; Phase 4 not started)

This file is the hand-off point for any new session. Read it first, then
`docs/ClearCase_Cursor_Prompt.md` (the specification) and `docs/decisions.md`.

## Phase progress

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Repository setup, MongoDB, upload, extraction, PDF viewing | **Complete and verified** |
| 2 | Search, candidate detection, manual review, real export, verification | **Complete and verified** |
| 3 | Real MCP server/client integration with tested tools | **Complete and verified** (branch `phase-3-mcp`, not yet committed/merged) |
| 4 | Policy ingestion, embeddings, retrieval, source citations | Not started |
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
- Download blocked if required checks fail or the export is stale.
- Filenames never include original sensitive text.

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

Frontend:
- Review / Search / Text / Export tabs.
- Findings panel with category checkboxes, Detect & propose, filters, per-item
  approve/reject/adjust, "all N matching", Approve/Reject shown, Draw region.
- Search panel proposes one occurrence or all matches.
- Overlay highlights findings and search hits via `bboxToCss`; draw mode uses
  `cssToBbox` (round-trip tested for zoom/rotation).
- Export panel: authorize + export, check list, download link, honest scope.

## Test results (actually run)

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
  return `transport_error`. Phase 5 should start one server per agent run.
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
- `requirements.lock` was generated on Python 3.14 but the backend image uses
  Python 3.12; the image built and the app ran, but keep an eye on it when
  Phase 3–5 add dependencies (e.g. sentence-transformers / torch wheels).
- Ports 8000 and 8001 are used by other projects; run ClearCase with
  `CLEARCASE_PORT=8010` and `VITE_API_PROXY_TARGET=http://127.0.0.1:8010`.
- spaCy NER is pretrained and imperfect: it tagged "MISCELLANEOUS NOTE" as a
  person on the sample. That is expected and is why review is required.
- Starlette warns that `httpx` with `TestClient` is deprecated in favour of
  `httpx2`; tests pass today. Evaluate switching in Phase 6.

## Remaining work (by phase)

**Phase 4 — RAG**: policy ingestion (Markdown chunking with policy id, title,
version, section, source, chunk id), sentence-transformers embeddings (model +
version tracked, rebuild on change), cosine retrieval in Python filtered by policy
version/owner, citation-id validation, "policy does not address this" handling,
clickable passages in the UI. Then replace the `retrieve_policy_guidance` stub and
let `propose_redactions` accept `policy_refs` **only** if they are real chunk ids.

**Phase 5 — Agent**: bounded tool loop using `McpToolClient`, one hosted provider
via its official SDK (verify model ids against current docs first), demo mode with
scripted commands, persisted conversations/messages/agent_runs/tool_events,
prompt-injection resistance in the system prompt (the structural part is done),
no chain-of-thought storage, Chat tab, "excerpts are sent to a hosted provider"
notice, and a reviewer-visible **Authorize export** hand-off message.

**Phase 6 — QA & docs**: Playwright E2E (upload → search/analyse → review →
approve → export → reopen conversation), synthetic evaluation, demo script,
interview guide, evaluation report, polish, full Compose re-run.

**Known gaps in Phase 3 (deliberate or deferred)**:
- No server-side per-tool timeout (tools are threads; the client enforces it).
- The UI does not call MCP yet (Phase 5).
- Tool results include document text snippets by design (the model needs them);
  Phase 5 must show the user when text goes to a hosted provider.

## Next implementation step

Start **Phase 4** (policy ingestion and retrieval):

1. Check `sentence-transformers` / PyTorch compatibility for Python 3.14 (host)
   *and* 3.12 (Docker image); pick a small maintained embedding model and verify
   its identifier and licence against its model card before pinning. Keep model
   downloads out of the image build (cache in a volume).
2. `policies` + `policy_chunks` collections, Markdown section chunker, seed
   `sample_data/policies/fictional_redaction_policy.md` (labelled fictional).
3. Retrieval interface (so a vector index can replace cosine-in-Python later),
   owner + policy-version filter before ranking, embeddings-unavailable state
   that never relabels keyword search as semantic.
4. Citation validation + `GET` endpoint to read a chunk; wire `retrieve_policy_guidance`.

## How to resume quickly

```bash
scripts/dev_mongo.sh start
scripts/download_models.sh                          # no-op if already installed
CLEARCASE_PORT=8010 scripts/dev_backend.sh          # terminal 1
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh   # terminal 2
cd backend && .venv/bin/python -m pytest -q
```
