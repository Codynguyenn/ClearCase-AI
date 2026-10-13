# ClearCase

AI-assisted legal document redaction — a local portfolio MVP demonstrating real
MCP tool use, retrieval-augmented generation over redaction policies, PDF
processing with true content removal, and MongoDB as the system of record.

> **Status: Phase 2 of 6 complete** (upload, extraction, search, detection,
> review, real export and verification). See [`PROJECT_STATUS.md`](PROJECT_STATUS.md)
> for what works, test results and the next step. The full specification lives in
> [`docs/ClearCase_Cursor_Prompt.md`](docs/ClearCase_Cursor_Prompt.md).

ClearCase does **not** guarantee legal compliance or detect every sensitive
item. Sample documents and the seeded policy are synthetic and fictional.

## What works today (Phases 1–2)

- Upload a text-based PDF; the file's **bytes** are validated (not its name or
  content-type). Encrypted, malformed, empty, oversized and over-long files are
  rejected with specific error codes.
- The original is stored unchanged in **MongoDB GridFS** with a SHA-256 hash.
- Text is extracted per page with **word-level rectangles**, and every word's
  character offsets map back into the page text. Search, detection and export
  all use that map — a language model never invents coordinates.
- Pages are classified `text` / `scanned` / `mixed` / `empty`. Scanned or mixed
  pages are flagged as **unsupported** rather than silently analysed.
- Literal search with case / whole-word options, occurrence ids, and highlights
  that stay aligned when you zoom or rotate.
- Candidate detection: validated patterns (email, US phone, SSN-format, street
  and postal lines) plus pretrained spaCy NER for people / organisations /
  locations. Detection **proposes**; it never auto-approves. Person and
  organisation stay separate categories so "keep company names" works.
- Review: approve / reject / adjust per occurrence, "all matching", bulk
  decisions, and a reviewer-drawn manual region. Every change bumps a review
  revision so stale export tokens die.
- Real export: PyMuPDF `apply_redactions` (text deleted, image pixels blanked),
  full rewrite, metadata/attachment scrub, then independent verification
  (text + rendered black boxes). Download is blocked if required checks fail.
  Export requires a 5-minute, hash-pinned authorization the model cannot create.
- Deleting a document removes pages, findings, revisions, exports, GridFS files
  and audit payloads.
- 144 backend tests against a **real MongoDB**; 11 frontend geometry tests.

## Stack

| Area | Choice | Pinned |
| --- | --- | --- |
| Frontend | React 19, TypeScript, Vite 8, Tailwind CSS 4, `react-pdf` (PDF.js) | `frontend/package-lock.json` |
| Backend | Python 3.11+, FastAPI, Pydantic v2, `pymongo` (official), PyMuPDF, spaCy | `backend/requirements.lock` |
| Database | MongoDB 8.0 Community + GridFS | — |
| Tests | pytest (backend), vitest (frontend geometry), Playwright (Phase 6) | — |
| Later phases | official `mcp` SDK, sentence-transformers, hosted LLM | — |

### Licensing note (important)

PyMuPDF is dual-licensed **AGPL-3.0 / commercial**. Because ClearCase depends on
it, this repository is licensed under **AGPL-3.0** (`LICENSE`). Anyone offering a
modified ClearCase as a network service must make their source available. A
closed-source product would need Artifex's commercial licence or another PDF
library. Details: `docs/decisions.md` §1.

## Setup (Apple Silicon Mac, native)

Requirements: Python 3.11+ (3.14 tested), Node ≥ 22.12 (a helper is provided
if yours is older), `curl`. No Docker or Homebrew needed.

```bash
# 1. MongoDB — downloads the official community binary once into .local/ and runs it on localhost
scripts/dev_mongo.sh start

# 2. Backend — creates backend/.venv, installs pinned deps, serves http://127.0.0.1:8000
scripts/dev_backend.sh

# 3. Frontend (new terminal) — installs deps, serves http://127.0.0.1:5173 and proxies /api
scripts/dev_frontend.sh

# 4. (Recommended) pretrained English NER model (~12 MB). Detection works without it.
scripts/download_models.sh

# 5. (Optional) regenerate the synthetic sample PDF
backend/.venv/bin/python sample_data/generate_samples.py
```

Open <http://127.0.0.1:5173>, upload `sample_data/sample_contract.pdf`.

Configuration is via environment variables or a root `.env` file; copy
`.env.example` and read the comments. Everything has a safe local default.

**Port already in use?** Set `CLEARCASE_PORT=8010 scripts/dev_backend.sh` and
`VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh`.

### Setup with Docker Compose

```bash
docker compose up mongo            # just the database, run backend/frontend natively
docker compose up --build          # the whole stack
```

## Running the tests

The backend tests need a reachable MongoDB (they use a separate
`clearcase_test` database and drop it before every test).

```bash
scripts/dev_mongo.sh start
cd backend && .venv/bin/python -m pytest -q
```

Frontend checks:

```bash
cd frontend && npm run lint && npm test && npm run build
```

CI (`.github/workflows/ci.yml`) runs both jobs with a MongoDB service container.

## API (Phases 1–2)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Backend, MongoDB and NER availability |
| `POST` | `/api/documents` | Upload a PDF (multipart `file`), returns `202` and schedules extraction |
| `GET` | `/api/documents` | List documents, newest first |
| `GET` | `/api/documents/{id}` | Document record incl. status, extraction info, job status |
| `POST` | `/api/documents/{id}/reprocess` | Retry extraction |
| `GET` | `/api/documents/{id}/pages` | Page summaries (geometry, kind, counts) |
| `GET` | `/api/documents/{id}/pages/{n}` | Page text + words with rectangles |
| `GET` | `/api/documents/{id}/search` | Literal search (`q`, `case_sensitive`, `whole_word`) |
| `GET` | `/api/documents/{id}/file` | Stream the untouched original PDF |
| `POST` | `/api/documents/{id}/detect` | Preview detector candidates (stores nothing) |
| `GET` | `/api/documents/{id}/findings` | List findings (filter by status/category/page) |
| `POST` | `/api/documents/{id}/findings/propose-detected` | Turn selected detector categories into *proposed* findings |
| `POST` | `/api/documents/{id}/findings/propose-search` | Propose matches of a search (one occurrence or all) |
| `POST` | `/api/documents/{id}/findings/manual` | Reviewer-drawn region (created approved) |
| `POST` | `/api/documents/{id}/findings/{fid}/decision` | Approve / reject / reopen; optional `apply_to_all_matching` |
| `POST` | `/api/documents/{id}/findings/bulk-decision` | Decide many findings at once |
| `PUT` | `/api/documents/{id}/findings/{fid}/boxes` | Adjust rectangles; finding returns to proposed |
| `GET` | `/api/documents/{id}/review-summary` | Counts, blockers, warnings |
| `POST` | `/api/documents/{id}/export-authorizations` | Short-lived reviewer grant (5 minutes) |
| `POST` | `/api/documents/{id}/exports` | Redact, verify, store only if required checks pass |
| `GET` | `/api/documents/{id}/exports` | List exports |
| `GET` | `/api/documents/{id}/exports/{eid}/file` | Download a verified, non-stale export |
| `DELETE` | `/api/documents/{id}` | Delete document and everything derived from it |

Errors always have the shape `{"error": {"code": "...", "message": "..."}}`.
Interactive docs: <http://127.0.0.1:8000/docs>.

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the Mermaid diagram,
layer responsibilities and data model, and [`docs/decisions.md`](docs/decisions.md)
for the reasoning behind each major choice.

## Limitations (current)

- Only English, text-based PDFs. No OCR: scanned/mixed pages are flagged, not read.
- Text inside images, annotations and form fields is not extracted.
- Single server-controlled demo identity; **not** authentication. Localhost only.
- Extraction and export run inside the API process, suitable for a local MVP.
- NER is a pretrained model we did not train; it produces false positives
  (for example "MISCELLANEOUS NOTE" tagged as a person). That is why every
  finding needs a human decision and why we never invent a confidence score.
- MCP, RAG, the assistant and the Playwright end-to-end test are later phases.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Red banner "backend is not reachable" | Start `scripts/dev_backend.sh`; if you changed the port, set `VITE_API_PROXY_TARGET`. |
| Red banner "MongoDB is unreachable" / `/api/health` shows `"mongodb": false` | `scripts/dev_mongo.sh start` (or `docker compose up mongo`). Logs: `.local/mongod.log`. |
| `vite build` → "Cannot find native binding" | Node < 22.12; npm skipped the optional native package. Run `eval "$(scripts/dev_node.sh)"` then `rm -rf node_modules package-lock.json && npm install`. |
| `CERTIFICATE_VERIFY_FAILED` from Python scripts that fetch URLs | python.org builds need `Install Certificates.command` run once (not needed by ClearCase itself). |
| Upload rejected with `encrypted_pdf` | Remove the password from the PDF and upload again. |
| Document stuck in "Processing" after a crash | On the next backend start it is marked `error`; click **Retry processing**. |
| Tests fail with "MongoDB is required" | Start MongoDB first; tests intentionally do not fall back to a mock. |
| NER tests skipped / people not detected | Run `scripts/download_models.sh` and restart the backend. Pattern detection still works. |
| Export button disabled | Every proposal must be approved or rejected. Check the Review tab badge. |

## Repository layout

```text
backend/app/api            HTTP routes
backend/app/services       validation, extraction, document lifecycle
backend/app/repositories   MongoDB + GridFS access (ownership-checked)
backend/app/models         Pydantic schemas
backend/app/agent          (Phase 5) tool-calling loop
backend/app/mcp_server     (Phase 3) MCP server over stdio
backend/tests              pytest suite
frontend/src               React app (api/, hooks/, components/, lib/)
sample_data/               synthetic contract generator + fictional policy
evaluation/                (Phase 6) synthetic evaluation
docs/                      spec, architecture, decisions
scripts/                   dev_mongo.sh, dev_node.sh, dev_backend.sh, dev_frontend.sh
```
