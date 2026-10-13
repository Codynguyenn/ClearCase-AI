# Design decisions (plain English)

Short records of the choices that shape the project, written so they can be
explained in an interview. Newest at the bottom.

## 1. PyMuPDF for PDF work, and what its licence means for this repo

**Choice.** PyMuPDF (`pymupdf`) handles extraction, coordinates and, in Phase 2,
real redaction.

**Why.** It gives word-level rectangles directly, supports true content removal
(`Page.apply_redactions`), and is actively maintained with Python 3.14 wheels.

**Licence implication.** PyMuPDF is dual-licensed: **GNU AGPL v3** or a
commercial licence from Artifex. Because ClearCase links to it, the project is
released under **AGPL-3.0** (see `LICENSE`). In practice: anyone who runs a
modified version as a network service must offer its source. For a portfolio
project that is fine; a company shipping a closed product would need the
commercial licence or a different library.

## 2. Synchronous `pymongo` rather than an async driver

FastAPI runs ordinary `def` endpoints in a thread pool, so the official
synchronous driver is simple and fast enough for a local, single-user tool. It
avoids a second driver and makes the repositories trivially reusable from the
MCP server process, which is also synchronous Python. If the app ever needs
high concurrency, `pymongo`'s own async API is the upgrade path.

## 3. Document metadata, page text and PDF bytes are stored separately

- `documents` holds small metadata so lists are cheap.
- `pages` holds text + word rectangles, one record per page, so a 200-page file
  never approaches MongoDB's 16 MB document limit and page fetches are indexed.
- PDF bytes live in **GridFS**; records only hold the file id. Base64 inside
  metadata records is explicitly avoided.

## 4. Page text is built from words, so character offsets are exact

Instead of storing PyMuPDF's free-form text and separately storing words, the
page text is *constructed* by joining the words (space within a line, newline
between lines) while recording each word's `char_start`/`char_end`. Searching
the text and mapping a hit back to rectangles is then a plain index lookup —
no fuzzy re-alignment, and no asking a language model for coordinates.

## 5. Scanned / mixed pages are flagged, never silently skipped

Heuristics: a page with no words and ≥50% image coverage is `scanned`; a page
with words and ≥30% image coverage is `mixed`; no words and little imagery is
`empty`. Anything other than `text` is **unsupported**. The document status
becomes `unsupported` and the UI lists the affected pages. Text pages in the
same document remain viewable and searchable. OCR is a documented extension,
not a hidden fallback.

## 6. Validation inspects the bytes, not the filename

Browsers set `Content-Type` from the extension, and extensions are trivially
wrong. Validation checks the `%PDF-` header, opens the file with PyMuPDF, and
rejects encrypted, empty, malformed or oversized files with a stable error code
(`encrypted_pdf`, `not_a_pdf`, ...). The filename is sanitised before storage
and is never used to touch the filesystem.

## 7. Demo identity is not authentication — but ownership checks exist from day one

Every repository query and GridFS read includes `owner_id`. Today that id comes
from one server-side setting (`CLEARCASE_DEMO_OWNER_ID`). Real authentication
later only has to change `get_owner_id()` in `api/deps.py`. The server binds to
localhost. This local mode is **not** safe for a multi-user deployment.

## 8. Processing runs as a background task with persisted status

Upload returns `202` with `status: uploaded`; extraction runs in a FastAPI
background task and writes `processing`/`ready`/`unsupported`/`error` to the
document record. On startup, documents stuck in `processing` (a crash mid-job)
are marked `error` with an explanatory message and can be retried via
`POST /documents/{id}/reprocess`. A queue could replace the background task
later without changing the state machine.

## 9. Tests use a real MongoDB, not a mock

GridFS semantics, unique indexes and ownership filters are part of what needs
testing. The test suite uses a dedicated `clearcase_test` database that is
dropped before each test. Without a reachable MongoDB the tests fail with a
clear message rather than silently passing against a fake.

## 10. Local MongoDB and Node without Docker or Homebrew

The development machine has neither. `scripts/dev_mongo.sh` downloads the
official MongoDB Community tarball for macOS arm64 into git-ignored `.local/`
and runs `mongod` bound to localhost. `scripts/dev_node.sh` does the same for
Node 22 LTS when the system Node is older than 22.12 (Vite 8's bundler needs
it; with an older Node, npm silently drops the native binding). Docker Compose
remains the documented alternative.

## 11. Rotation handled with one explicit transform

PyMuPDF reports rectangles in unrotated page space; `react-pdf`'s `rotate` prop
*replaces* the page's intrinsic rotation. The viewer therefore passes
`(page.rotation + userRotation) % 360` to `react-pdf` and to
`bboxToCss()` so both always agree. The "Show word boxes" toggle exists to make
misalignment visible immediately.

## 12. Search is literal, not regex

User text is escaped. Whitespace in the query matches any run of spaces or
newlines so a wrapped name still matches. We never compile a user-supplied
regular expression (catastrophic backtracking, and "search" should mean
"find this text"). Result size and query length are capped.

## 13. Detection produces candidates; only the reviewer approves

Patterns (email, NANP phone, SSA-plausible SSN, street / postal lines) and
pretrained spaCy NER (`en_core_web_sm`) write **proposed** findings. The model
and MCP tools (Phase 3+) will only be given `propose`. Person vs organisation
are separate categories so "keep company names" is a filter, not a hope.
Detectors have no calibrated confidence, so we show `format_valid` or
`review_needed` plus the source string — never a fake probability.

## 14. The server, not the client, derives rectangles

Callers send a page number and a character span. The server re-reads the stored
page, checks the text still matches, and computes boxes via `locate_span`.
Client- or model-supplied coordinates are never trusted. That is the same
reason we built page text *from* words in Phase 1.

## 15. Export is authorize → redact → verify → store

A 5-minute token is hashed (SHA-256), pinned to the document hash, review
revision and the exact approved rectangles, and consumed atomically. Redaction
uses PyMuPDF's real `apply_redactions` (text deleted, image pixels blanked)
plus a full rewrite (`garbage=4`). Verification re-opens the output and checks
text-in-box, rendered blackness, metadata, single `%%EOF`, and remaining copies
of approved text (rejected findings count as "kept on purpose"). Required
checks must pass before a file is stored or downloadable.

## 16. spaCy is optional at runtime, pinned as a dependency

`spacy==3.8.16` is in the lockfile. The `en_core_web_sm` *model* is a separate
one-time download (`scripts/download_models.sh`). If the model is missing,
detection continues with patterns and says so; it never pretends NER ran.
