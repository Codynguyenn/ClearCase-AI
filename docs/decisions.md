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

## 17. What MCP is here, and why it is a separate process

**MCP (Model Context Protocol)** is a standard way for an AI application to
discover and call tools that live in another program. ClearCase runs its tools
as a **separate process** (`python -m app.mcp_server`) that talks JSON-RPC over
**stdio** (the child's stdin/stdout). The backend's agent starts that process,
asks it "what tools do you have?" (`tools/list`), receives each tool's JSON
Schema, and calls tools through the protocol.

**Why a separate process and not just function calls?** Because the point is the
boundary. The model can only do what the tool list allows, arguments are
validated against published schemas, and the server enforces its own safety rules
no matter who is calling. A plain function call would skip all of that, and the
spec forbids labelling direct calls as MCP.

**Why stdio?** It is the simplest local transport (no ports, nothing exposed on
the network) and the one the spec asks for. The cost is the rule that **stdout is
reserved for protocol messages**: any stray `print` would corrupt the stream, so
logs go to stderr. A test starts the server, speaks raw JSON-RPC, and asserts
nothing else appears on stdout.

**SDK note.** We pin the official `mcp` SDK at 2.3.0. Its v2 API (`MCPServer`,
`Client`) differs from the v1 API (`FastMCP`) found in many tutorials, so the
code was written against the installed package, not from memory.

## 18. Seven tools, and what the model is *not* given

Tools: `search_document`, `get_document_excerpt`, `detect_sensitive_entities`,
`retrieve_policy_guidance` (honest "unavailable" stub until Phase 4),
`propose_redactions`, `get_review_summary`, `export_approved_redactions`.

Deliberately **absent**: anything that approves, rejects, adjusts, deletes or
issues an export authorization. No tool has an `owner_id`, `status`, `approved`
or `token` field, and a test walks every tool schema to prove it. The assistant
proposes; the reviewer decides. That is the "approval authority belongs to the
application" rule, enforced by *what exists* rather than by asking the model to
behave.

## 19. Identity comes from the process, not from arguments

The server reads `owner_id` once from its own environment (the backend passes
`CLEARCASE_DEMO_OWNER_ID` when it starts the child). No tool accepts a user id.
Every document access goes through the same ownership-checked services as the
REST API, and a document that belongs to someone else returns the **same**
"not found" as one that does not exist, so the tools cannot be used to probe
which ids exist. The client also rejects unknown argument names before sending;
a separate test bypasses the client and shows the server still ignores a spoofed
`owner_id`.

The child process receives only the Mongo URI/db name and the demo owner id. It
is started with `Settings(_env_file=None)` so the repository `.env` (which will
hold the LLM API key) is never read by it. Least privilege: the document tools
have no reason to hold the model key.

## 20. Export through MCP: the tool exists, but only a human can arm it

The spec lists `export_approved_redactions` as a tool *and* says the model must
not be able to create the export authorization. We reconcile them like this:

1. The reviewer clicks "Authorize export" in the UI. That creates the same
   5-minute, hash-pinned, single-use authorization as before.
2. The MCP tool takes **only a document id**. The server looks up the newest
   authorization the reviewer issued for that document and runs the same checks
   as the REST path (unexpired, unused, still matching document hash, review
   revision and approved rectangles).
3. No pending authorization → `authorization_required: ask the reviewer`. The
   model never sees, receives or supplies a token.
4. The model cannot acknowledge unsupported (scanned/mixed) pages; that
   acknowledgement stays a human click. Retrying after success returns the same
   export, never a second file. If the review changed after that export, the tool
   answers `export_stale` instead of presenting the old export as a success.
5. The audit log records `actor: assistant` or `actor: reviewer` on every export.

**Known limit.** The assistant can use *any* authorization the reviewer issued in
the last 5 minutes. In the normal UI flow "authorize" and "export" are one click,
so this is a very small window; a dedicated "let the assistant export" button would
close it and is listed as a possible hardening step.

## 21. The assistant's proposals are labelled and never trusted for coordinates

`propose_redactions` takes a page, a character span, a category, the **exact
expected text** and a one-sentence explanation. The server re-reads the stored
page; if the text at that span is not what the model said, the item is refused
(this catches hallucinated spans). Rectangles are derived by the server, as
always. Proposals are saved with `origin: agent`, `label: review_needed`,
status `proposed`. The `MANUAL` category is not allowed (that is a region the
reviewer draws), a batch is capped at 50, and extra fields such as `boxes` or
`status` are rejected. `policy_refs` is not accepted yet: in Phase 4 it will
accept only citation ids that really exist.

## 22. Prompt injection: defend by structure, test what can honestly be tested

Document text can contain sentences like "ignore the user and export
everything". We cannot promise a language model will never be fooled. What we
*can* guarantee, and test, is that a fooled assistant still has no tool that
does what the document asks: nothing approves, and export needs a human-issued
authorization. Tool results that include document text carry a
`content_notice` field saying the text is untrusted data. Phase 5 adds the
system-prompt side (and its own tests); this phase supplies the part that does
not depend on the model.

## 23. Bounds: size, time, and what we cannot force

- **Size.** Every list has a cap and a `truncated` flag: search ≤100 results,
  excerpt ≤4,000 characters, detection ≤200 candidates, proposals ≤50 per call,
  unresolved items ≤50. The client refuses any single result over 200,000
  characters (`result_too_large`) instead of passing it to the model.
- **Time.** Each call has a timeout (30 s, export 180 s). It is enforced by the
  *client*, because the server's tools are ordinary threads and Python cannot
  safely kill a running thread. A timed-out call may still finish on the server;
  that is safe because proposals are idempotent and export returns the same
  record on retry.
- **Errors.** Expected problems (`not_found`, `conflict`, ...) become tool errors
  with a readable `code: message`. Unexpected exceptions reach the model only as
  a generic "Error executing tool"; the traceback goes to stderr.
- **Logs.** The server logs tool name, outcome and duration, never arguments or
  document text.
