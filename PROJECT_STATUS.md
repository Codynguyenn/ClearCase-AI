# ClearCase — project status

Last updated: 2026-10-13 (end of Phase 2)

This file is the hand-off point for any new session. Read it first, then
`docs/ClearCase_Cursor_Prompt.md` (the specification) and `docs/decisions.md`.

## Phase progress

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Repository setup, MongoDB, upload, extraction, PDF viewing | **Complete and verified** |
| 2 | Search, candidate detection, manual review, real export, verification | **Complete and verified** |
| 3 | Real MCP server/client integration with tested tools | Not started |
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

Not yet tested: Playwright E2E (Phase 6), Docker Compose build, GitHub Actions
run (repo has no remote yet).

## Known issues / notes

- Dev machine: Python 3.14.7, Node 22.11.0 (below Vite 8's ≥22.12), no Docker,
  no Homebrew. Resolved with `.local/` downloads (git-ignored).
- Ports 8000 and 8001 are used by other projects; run ClearCase with
  `CLEARCASE_PORT=8010` and `VITE_API_PROXY_TARGET=http://127.0.0.1:8010`.
- spaCy NER is pretrained and imperfect: it tagged "MISCELLANEOUS NOTE" as a
  person on the sample. That is expected and is why review is required.
- Starlette warns that `httpx` with `TestClient` is deprecated in favour of
  `httpx2`; tests pass today. Evaluate switching in Phase 6.
- Docker Desktop is still the recommended install after Phase 2 (now).

## Remaining work (by phase)

**Phase 3 — MCP**: stdio server (`app/mcp_server`) with the seven tools and
Pydantic schemas, size limits/timeouts, ownership validation in the tool layer;
backend MCP client session; integration test that lists tools and calls
`search_document` through the protocol. Approval and export-authorization stay
reviewer-only (never exposed as model-callable tools that can set `approved`).

**Phase 4 — RAG**: policy ingestion (Markdown chunking with ids/sections),
sentence-transformers embeddings (model + version tracked), cosine retrieval in
Python filtered by policy version/owner, citation validation, "policy does not
address this" handling, clickable passages.

**Phase 5 — Agent**: bounded tool loop with one hosted provider via official
SDK (verify model ids against docs first), demo mode with scripted commands,
persisted conversations/messages/agent_runs/tool_events, prompt-injection
resistance, no chain-of-thought storage. Chat tab on the review screen.

**Phase 6 — QA & docs**: Playwright E2E (upload → search/analyse → review →
approve → export → reopen conversation), synthetic evaluation, demo script,
interview guide, evaluation report, polish.

## Next implementation step

Start **Phase 3** with a real MCP server over stdio:

1. Confirm current `mcp` Python SDK (already noted as 2.3.0 on PyPI) APIs
   against official docs; pin it.
2. `backend/app/mcp_server/` exposing tools: `search_document`,
   `get_document_excerpt`, `detect_sensitive_entities`,
   `retrieve_policy_guidance` (stub until Phase 4), `propose_redactions`
   (writes proposed only), `get_review_summary`. Do **not** expose decide/approve
   or `issue_authorization`.
3. Input/output Pydantic schemas, result-size limits, timeouts, ownership
   checks inside the tool layer (never trust a model-supplied user id).
4. Integration test: start the server, list tools, call `search_document`
   through the protocol.
5. Backend MCP client session the Phase 5 agent will use.

## How to resume quickly

```bash
scripts/dev_mongo.sh start
scripts/download_models.sh                          # no-op if already installed
CLEARCASE_PORT=8010 scripts/dev_backend.sh          # terminal 1
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh   # terminal 2
cd backend && .venv/bin/python -m pytest -q
```

**Now is a good time to install Docker Desktop** if you want
`docker compose up --build` tested before Phase 3 adds more dependencies.
