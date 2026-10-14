# ClearCase

AI-assisted legal document redaction — a local portfolio MVP demonstrating real
MCP tool use, retrieval-augmented generation over redaction policies, PDF
processing with true content removal, and MongoDB as the system of record.

> **Status: Phase 4 of 6 complete** (upload, extraction, search, detection,
> review, real export and verification, a real MCP server/client, and policy
> retrieval with local embeddings and clickable citations). See [`PROJECT_STATUS.md`](PROJECT_STATUS.md)
> for what works, test results and the next step. The full specification lives in
> [`docs/ClearCase_Cursor_Prompt.md`](docs/ClearCase_Cursor_Prompt.md).

ClearCase does **not** guarantee legal compliance or detect every sensitive
item. Sample documents and the seeded policy are synthetic and fictional.

## What works today (Phases 1–4)

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

### Policy library and retrieval (Phase 4)

- Upload Markdown/plain-text policies (or load the two **fictional** sample policies
  with one click). Each is split into passages with stable citation ids such as
  `northwind-redaction-policy:1.0:3`.
- A local pretrained embedding model (`all-MiniLM-L6-v2`, pinned revision) turns
  passages into vectors stored in MongoDB with the model name and revision.
- **Ask the policy** in the Policy tab: you get the matching passages, each with its
  similarity score and a clickable citation, or the plain statement that the active
  policies do not address the question. Nothing is invented and nothing is shown below
  the cutoff.
- Findings show the policy passages they cite; clicking a citation opens the real text.
- Policy versions: one is active, older ones stay readable and can be re-activated.
- If the model is missing, semantic search says it is **unavailable** (with the reason);
  it never quietly switches to keyword search.
- The assistant's MCP tool `retrieve_policy_guidance` uses the same retrieval, and
  `propose_redactions` accepts only citations that really exist.

Honest numbers (synthetic dev set, see `docs/decisions.md` #30): the right passage is
ranked first for 19 of 25 answerable questions, and the cutoff correctly refuses all
10 questions the policies do not cover, at the cost of wrongly turning away 8 of the
25 answerable ones. It is a small, easy, same-author test, not real-world accuracy.

## Stack

| Area | Choice | Pinned |
| --- | --- | --- |
| Frontend | React 19, TypeScript, Vite 8, Tailwind CSS 4, `react-pdf` (PDF.js) | `frontend/package-lock.json` |
| Backend | Python 3.11+, FastAPI, Pydantic v2, `pymongo` (official), PyMuPDF, spaCy | `backend/requirements.lock` |
| Embeddings (RAG) | `sentence-transformers` 6.1.0 + CPU-only PyTorch 2.14.1, model `all-MiniLM-L6-v2` (Apache-2.0) at a pinned revision, NumPy | `backend/requirements.lock` |
| Database | MongoDB 8.0 Community + GridFS | — |
| Tests | pytest (backend), vitest (frontend geometry), Playwright (Phase 6) | — |
| MCP | official `mcp` SDK 2.3.0 (stdio server + client), `jsonschema` | `backend/requirements.lock` |
| Later phases | hosted LLM provider (Phase 5) | — |

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

# 4. (Recommended) one-time model downloads: spaCy English NER (~12 MB) and the policy-search
#    embedding model (~90 MB). The app works without them and says exactly what is unavailable.
scripts/download_models.sh

# 5. (Optional) regenerate the synthetic sample PDF
backend/.venv/bin/python sample_data/generate_samples.py
```

Open <http://127.0.0.1:5173>, upload `sample_data/sample_contract.pdf`, then open the
**Policy** tab and click **Load the fictional sample policies**.

**Disk space:** PyTorch and the embedding libraries add about 1 GB to `backend/.venv`
(plus the 87 MB model in `.local/models`). The pip download cache can briefly use
~0.5 GB more.

Configuration is via environment variables or a root `.env` file; copy
`.env.example` and read the comments. Everything has a safe local default.

**Port already in use?** Set `CLEARCASE_PORT=8010 scripts/dev_backend.sh` and
`VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh`.

### Setup with Docker Compose

```bash
docker compose up mongo            # just the database, run backend/frontend natively
docker compose up --build          # the whole stack
# ONCE: download the policy-search model into the `models` volume (it is never baked into the image)
docker compose run --rm backend python -m app.cli download-embedding-model
```

The backend image is about 2.3 GB (CPU-only PyTorch, no CUDA). The two fictional sample
policies are mounted read-only from `sample_data/` and loaded from the Policy tab.

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

## API (Phases 1–4)

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
| `GET` | `/api/policies` | List policies with version, status and embedding state |
| `POST` | `/api/policies` | Upload a `.md` / `.txt` policy (multipart `file`); it becomes the active version |
| `POST` | `/api/policies/seed-samples` | Load the fictional sample policies (`{"reset": true}` rebuilds them) |
| `POST` | `/api/policies/search` | Ask the active policies (`query`, `top_k`); passages above the cutoff, or "does not address" |
| `GET` | `/api/policies/passages/{citation_id}` | Read the real text behind a citation |
| `POST` | `/api/policies/passages/resolve` | Check many citation ids at once (unknown ones are reported missing) |
| `POST` | `/api/policies/{pid}/versions/{v}/activate` | Make a version the one that is searched |
| `POST` | `/api/policies/{pid}/versions/{v}/reindex` | Embed again (after a model change or a failed earlier attempt) |
| `POST` | `/api/policies/reindex` | Re-index every policy that needs it |
| `DELETE` | `/api/policies/{pid}` (or `.../versions/{v}`) | Delete a policy and its passages |

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
- The assistant itself (the tool-calling loop and chat) and the Playwright end-to-end test are later phases.
- Policy retrieval is small-scale by design: brute-force cosine in Python over vectors
  in MongoDB (fine for a few thousand passages; use a vector index beyond that).
- The embedding model reads only the first 256 tokens of a passage (passages are kept
  well under that), understands English best, and its similarity cutoff (0.38) is a
  heuristic tuned on a small synthetic set: it will sometimes say "the policy does not
  address this" when it does (8 of 25 on the dev set). A human still decides.
- The first policy question after the app starts takes ~15-20 s while the model loads.
- The MCP server and client exist and are tested, but nothing in the UI calls them yet; that is Phase 5.

## MCP server (Phase 3)

A real Model Context Protocol server runs as its own process over stdio:

```bash
cd backend && .venv/bin/python -m app.mcp_server   # speaks JSON-RPC on stdin/stdout; logs to stderr
```

You normally never start it by hand: the backend's `McpToolClient`
(`backend/app/agent/mcp_client.py`) launches it, lists the tools and calls them.
Tests that do exactly that: `backend/tests/test_mcp_protocol.py` and
`test_mcp_client.py`. The tool list, safety rules and reasoning are in
[`docs/architecture.md`](docs/architecture.md) and `docs/decisions.md` (17–23).

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
| Policy tab says "search unavailable" / amber banner | The embedding model is missing or disabled. Run `scripts/download_models.sh` (Docker: `docker compose run --rm backend python -m app.cli download-embedding-model`), then click **Re-index** on each policy (no restart needed). |
| Policy shows "needs re-index" | It was embedded with a different model revision (or embedding failed). Click **Re-index**. |
| Policy upload says `policy_version_exists` | That version already exists with different text. Change the `version:` in the file's front matter and upload again. |
| "Policy search found nothing" for a question you expected to match | The cutoff is conservative (see Limitations). Rephrase with the policy's own words, or read the passages in the library. |
| MCP tool says `authorization_required` | By design the assistant cannot export on its own. Click **Authorize export** in the Export tab (valid 5 minutes), then ask again. |
| Export button disabled | Every proposal must be approved or rejected. Check the Review tab badge. |

## Repository layout

```text
backend/app/api            HTTP routes
backend/app/services       validation, extraction, document lifecycle
backend/app/repositories   MongoDB + GridFS access (ownership-checked)
backend/app/models         Pydantic schemas
backend/app/agent          mcp_client.py (Phase 3); tool-calling loop (Phase 5)
backend/app/mcp_server     MCP server over stdio: seven tools (Phase 3)
backend/tests              pytest suite
frontend/src               React app (api/, hooks/, components/, lib/)
sample_data/               synthetic contract generator + two fictional policies
evaluation/                retrieval dev set + tuning script (Phase 4); full evaluation in Phase 6
docs/                      spec, architecture, decisions
scripts/                   dev_mongo.sh, dev_node.sh, dev_backend.sh, dev_frontend.sh, download_models.sh
```
