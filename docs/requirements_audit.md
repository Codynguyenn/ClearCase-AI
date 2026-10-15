# Requirements audit

Every requirement of the specification (`docs/ClearCase_Cursor_Prompt.md`), with its status and the evidence for it. Written after a
line-by-line read of the specification on 2026-10-15. **Every file path and test name below is checked by a test**
(`backend/tests/test_requirements_audit.py`), so a renamed test or a deleted file breaks the build instead of silently making this
document false.

Status words: **Met** (implemented and tested); **Met, with a documented deviation** (the deviation is named); **Partly** (the gap is
named); **Deferred by the specification** (the specification itself defers it). "Evidence" is where to look, not a claim of real-world accuracy:
all evaluation data is synthetic.

## 1. Product and scope

| Requirement | Status | Evidence |
| --- | --- | --- |
| Upload an English, text-based PDF; view pages and searchable text | Met | `backend/tests/test_documents_api.py`, `frontend/e2e/review-flow.spec.ts` |
| Ask the assistant to find words, people, sensitive information | Met | `backend/tests/test_demo_assistant.py`, `backend/tests/test_agent_loop.py` (live mode: `scripts/live_check.py`) |
| Proposals with locations, reasons and supporting policy references | Met | `backend/tests/test_review_api.py`, `test_a_proposal_cannot_cite_another_owners_policy` |
| Approve, reject or adjust each proposal | Met | `backend/tests/test_review_api.py` |
| Download a PDF with approved information actually removed | Met | `backend/tests/test_redaction.py`, `backend/tests/test_export_api.py`; the browser test re-uploads the download and searches it |
| Reopen the document and continue the conversation after a restart | Met | `test_the_conversation_is_still_there_after_the_app_restarts`, `test_history_persists_across_app_restart`, `test_review_decisions_and_the_revision_survive_an_app_restart` |
| Synthetic contract and a clearly fictional policy; no compliance claim | Met | `sample_data/sample_contract.pdf`, `sample_data/policies/fictional_redaction_policy.md`, `README.md` |
| Scanned or mixed pages flagged, never silently reported as analysed | Met | `test_scanned_pdf_is_marked_unsupported_not_ready` |
| OCR, DOCX, handwriting, production hosting | Deferred by the specification | `README.md` (Limitations) |

## 2. Stack and architecture

| Requirement | Status | Evidence |
| --- | --- | --- |
| React, TypeScript, Vite, Tailwind; PDF.js wrapper | Met | `frontend/package.json` (react-pdf) |
| Python 3.11+, FastAPI, Pydantic | Met | `backend/pyproject.toml` |
| MongoDB with the official driver; GridFS for the PDFs | Met | `backend/app/db.py`, `docs/architecture.md` |
| PyMuPDF; official MCP SDK; spaCy NER plus patterns; sentence-transformers | Met | `backend/pyproject.toml`, `backend/requirements.lock` |
| pytest and Playwright; Docker Compose with a native option | Met | `docker-compose.yml`, `frontend/playwright.config.ts`, `README.md` |
| One backend, one frontend, MongoDB, one local MCP process | Met | `docs/architecture.md` |
| Check licences; pin versions; lockfiles; document the PDF library's licence | Met | `docs/dependency_licenses.md`, `docs/decisions.md` (1), `backend/requirements.lock`, `frontend/package-lock.json` |
| The suggested folder structure; layers testable independently | Met | `backend/app/services/pdf_processing.py`, `backend/app/services/detection.py`, `backend/app/services/retrieval.py` |

## 3. Document processing and review

| Requirement | Status | Evidence |
| --- | --- | --- |
| Validate the real file type, size and page count; reject encrypted or malformed files | Met | `test_rejects_encrypted_pdf`, `test_rejects_oversized_upload` |
| Original stored unchanged with a content hash; text by page with coordinates mapped to rectangles | Met | `backend/tests/test_pdf_processing.py` |
| Record extraction limitations and unsupported pages; show upload, processing, ready, unsupported, error | Met | `backend/tests/test_documents_api.py`, `frontend/src/components/DocumentHeader.tsx` |
| The model never invents coordinates; every span is verified against the stored document | Met | `backend/tests/test_review_api.py`, `backend/tests/test_mcp_protocol.py` |
| Review screen: list, viewer with navigation and zoom, findings and chat panels, filters, approve/reject/adjust/manual region, summary, export button | Met | `frontend/e2e/review-flow.spec.ts`, `frontend/src/components/FindingsPanel.test.tsx` |
| Highlights stay aligned when zooming or rotating | Met | `frontend/src/lib/geometry.test.ts` |
| Occurrence-specific decisions and an explicit "all matching" | Met | `backend/tests/test_review_api.py` |
| Findings store page, boxes, category, source, status, explanation, policy references, document version, timestamps | Met | `backend/app/models/findings.py` |
| Stable ids; overlapping proposals deduplicated without discarding conflicting categories | Met | `test_list_filters_and_overlaps` |

## 4. Sensitive-information detection

| Requirement | Status | Evidence |
| --- | --- | --- |
| Patterns (email, phone, SSN-format), pretrained NER, model-assisted interpretation | Met, with a documented deviation | `backend/tests/test_detection.py`; model-assisted interpretation exists in live mode only, demo mode is scripted by design |
| Format match distinguished from a verified identifier; no number treated as an account number | Met | `backend/tests/test_detection.py` |
| People and organisations separate, so "keep company names" works | Met | `evaluation/assistant_eval/REPORT_heldout_v2.md` (the models proposed no company name) |
| Detection produces candidates; never auto-redacts | Met | `backend/tests/test_review_api.py` |
| No fake probabilities; source and "review needed" instead | Met | `docs/decisions.md` (13) |
| Repeated text, punctuation, line breaks, Unicode; unmappable mentions flagged for manual review | Met | `test_search_handles_unicode_and_flags_unsupported_pages`, `test_line_breaks_and_curly_quotes_are_forgiven_nothing_else_is` |

## 5. Real MCP integration

| Requirement | Status | Evidence |
| --- | --- | --- |
| A real MCP server and a real client session over stdio, protocol stdout clean | Met | `backend/tests/test_mcp_protocol.py`, `backend/tests/test_mcp_client.py` |
| The seven tools with schemas, size limits, timeouts, meaningful errors | Met | `test_server_lists_exactly_the_documented_tools_with_schemas`, `test_invalid_arguments_are_rejected_with_useful_messages` |
| Integration test that starts the server, lists tools and calls `search_document` through the protocol | Met | `test_search_document_through_the_protocol_matches_the_rest_search` |
| Document access checked in the tool layer; no model-supplied user id, path, URL or query | Met | `test_other_owners_documents_look_like_documents_that_do_not_exist` |
| The model cannot approve; export needs a short-lived server-issued authorization tied to the document hash and review revision | Met | `backend/tests/test_export_api.py`, `docs/decisions.md` (20) |

## 6. Agent behaviour and model configuration

| Requirement | Status | Evidence |
| --- | --- | --- |
| A small bounded tool-calling loop; one hosted provider through its official SDK; adapter separate; key and model from the environment | Met | `backend/tests/test_agent_loop.py`, `backend/tests/test_anthropic_provider.py`, `.env.example` |
| Limits on rounds, time and context; handles rate limits, errors, cancellation, malformed arguments; retries create no duplicates | Met | `backend/tests/test_agent_loop.py`, `backend/tests/test_conversations_api.py` |
| Persisted activity record (tool, status, duration, sanitized summary); no private reasoning stored; no raw text or keys logged | Met | `backend/tests/test_conversations_api.py` |
| Document and policy text is data, not instructions | Met | `test_the_injected_sentence_reaches_the_model_labelled_as_untrusted_document_content`, `test_obeying_the_injection_without_a_reviewer_authorization_creates_nothing` |
| Cites real pages and policy chunks | Met | `backend/tests/test_citations.py` |

## 7. RAG over redaction guidelines

| Requirement | Status | Evidence |
| --- | --- | --- |
| Sensible chunks with policy id, title, version, section, source, chunk id; local embeddings stored in MongoDB | Met | `backend/tests/test_policy_chunking.py`, `backend/tests/test_retrieval_semantic.py` |
| Cosine similarity over embeddings behind an interface; scale limit documented | Met | `backend/app/services/retrieval.py`, `README.md` (Limitations) |
| Corpus separate from the reviewed documents; filtered by owner before ranking | Met, with a documented deviation | `test_weak_passages_never_include_another_owners_text`; every **active** policy version is searched, there is no per-document policy choice (`docs/decisions.md` 48) |
| Missing or weak evidence answered as "the policy does not address it"; cutoff derived from evaluation and described as a heuristic | Met | `docs/decisions.md` (30, 36, 53), `evaluation/retrieval2/REPORT.md` |
| Clickable citations that open the real passage; ids validated; model version tracked; embeddings rebuilt on change | Met | `backend/tests/test_policy_api.py`, `backend/tests/test_startup_reindex.py`, `frontend/src/components/PassageDialog.test.tsx` |
| Seeded policies labelled as fictional | Met | `sample_data/policies/fictional_redaction_policy.md` |

## 8. Non-relational database

| Requirement | Status | Evidence |
| --- | --- | --- |
| MongoDB as source of truth for documents, pages, conversations, separate message records, proposals, revisions, policies, runs, tool events, exports, audit | Met | `backend/app/db.py`, `backend/tests/test_conversations_repo.py` |
| Indexes for the real queries; PDFs in GridFS, not base64 | Met | `backend/app/db.py` |
| History persists across restarts; paginated; bounded history sent to the model | Met | `test_history_persists_across_app_restart`, `backend/tests/test_conversations_api.py` |
| Demo identity with ownership checks in repositories and MCP; not presented as authentication | Met | `docs/decisions.md` (7), `README.md` (Limitations) |
| Deletion of a document and everything derived from it | Met | `test_delete_removes_document_pages_and_file` |

## 9. PDF export correctness

| Requirement | Status | Evidence |
| --- | --- | --- |
| Real redaction (not an overlay); a fully rewritten new file; original kept separately | Met | `backend/tests/test_redaction.py` |
| Metadata, comments, attachments and other structures scrubbed where supported; image and vector content handled | Met, with a documented deviation | `backend/app/services/redaction.py` (image pixels blanked, line art removed where it touches); hidden or invisible text is not removed unless approved, and is reported as an unresolved copy |
| Export blocked or rejected when a feature cannot be sanitized reliably | Met | `backend/tests/test_export_warnings.py` |
| No sensitive text in filenames or summaries | Met | `backend/tests/test_export_api.py` |
| Approval tied to the document hash and review revision; stale authorizations invalidated | Met | `test_export_goes_stale_when_review_changes_later` |
| Reopen and verify; region-aware; remaining copies reported as intentionally retained or unresolved; text extraction plus rendered-page checks | Met | `test_rejected_findings_are_reported_as_intentionally_retained`, `evaluation/pipeline_eval/REPORT_v2.md` |
| Download prevented when required verification fails; supported scope described honestly | Met | `backend/tests/test_export_api.py`, `docs/decisions.md` (15) |

## 10. Demo mode, interface and operations

| Requirement | Status | Evidence |
| --- | --- | --- |
| Explicit demo mode with no paid API: real upload, extraction, search, review, persistence, MCP calls and export; labelled scripted commands | Met | `backend/tests/test_demo_assistant.py`, `frontend/e2e/review-flow.spec.ts` |
| One-time model downloads documented; embeddings unavailable is shown as unavailable, never relabelled keyword search | Met | `README.md`, `backend/tests/test_retrieval_semantic.py` |
| Live mode shows setup and connection errors; no silent fallback to simulated answers | Met | `backend/tests/test_conversations_api.py` |
| Readable, keyboard-accessible interface with empty states, loading, retry, errors | Met | `frontend/e2e/review-flow.spec.ts` (focus and Escape in the real browser), `frontend/src/components/HealthBanner.test.tsx` |
| Secrets server-side; `.env.example`; ignore rules; users told when excerpts go to a hosted provider | Met | `.env.example`, `.gitignore`, `frontend/src/components/ChatPanel.test.tsx` |
| Health checks; bounded processing jobs; persisted job status; recovery or retry after a crash | Partly | `test_interrupted_processing_is_recovered_on_startup`; the extraction time budget is checked between pages and cannot interrupt a single page that hangs inside PyMuPDF (`PROJECT_STATUS.md`, known gaps) |

## 11. Tests and evaluation

| Requirement | Status | Evidence |
| --- | --- | --- |
| Tests for each listed behaviour (search, company names, transforms, approve/reject and restart, MCP, invalid arguments and unauthorized ids, stale approvals, injection, removal and preservation, unsupported PDFs, citations and unanswerable questions) | Met | the tests named in sections 3, 5, 6, 7 and 9 above, plus `backend/tests/test_search.py`, `backend/tests/test_agent_injection.py` |
| Browser test: upload, search or analyse, review, approve, export, reopen the conversation | Met | `frontend/e2e/review-flow.spec.ts` |
| Reproducible synthetic dataset with several templates and ground-truth spans; development kept apart from held-out | Met | `evaluation/assistant_eval/dataset.py`, `backend/tests/test_assistant_eval.py` |
| Precision, recall, F1 by entity category; retrieval hit rate at k; export-verification outcomes; latency; matching rules and denominators | Met | `evaluation/assistant_eval/REPORT_heldout_v2.md`, `evaluation/retrieval2/REPORT.md`, `evaluation/pipeline_eval/REPORT_v2.md`, `evaluation/assistant_eval/PROTOCOL.md` |
| Pattern-only versus pattern plus NER; live model comparison; model and policy versions recorded | Met | `evaluation/assistant_eval/REPORT_heldout.md`, `test_every_generated_report_states_the_versions_in_its_header` |
| Synthetic performance treated as synthetic; no invented results | Met | `evaluation/README.md`; several of my own pre-registered predictions are reported as failed |

## 12. Deliverables

| Deliverable | Status | Evidence |
| --- | --- | --- |
| Source and pinned dependencies | Met | `backend/requirements.lock`, `frontend/package-lock.json` |
| Docker Compose and Apple Silicon setup commands | Met | `docker-compose.yml`, `README.md` |
| Synthetic samples, policies and a resettable seed script | Met | `backend/app/cli.py`, `backend/tests/test_seed_samples.py` |
| `.env.example` with explanations | Met | `.env.example` |
| Backend tests, the browser test and GitHub Actions checks | Met | `.github/workflows/ci.yml` |
| README with features, setup, usage, architecture, limitations, troubleshooting | Met | `README.md` |
| A compact Mermaid architecture diagram | Met | `docs/architecture.md` |
| Three-minute demo script; interview guide | Met | `docs/demo_script.md`, `docs/interview_guide.md` |
| An honest evaluation report of measurements actually obtained | Met | `evaluation/README.md` |
| `PROJECT_STATUS.md` with features, incomplete work, test results, next task | Met | `PROJECT_STATUS.md` |
| The six build phases, each verified before the next | Met | `PROJECT_STATUS.md` (phase table), `docs/decisions.md` |

## What "done" does not mean

All six phases and every deliverable are done, with the two named exceptions above (**Partly**: a page that hangs inside PyMuPDF is not
interrupted; **deviation**: all active policy versions are searched). It does **not** mean the application is production-ready or accurate on
real documents: the data is synthetic, the demo identity is not authentication (localhost only), the layout is a desktop one, only English
text-based PDFs are supported, and the similarity cutoff is a weak answer/no-answer decision (balanced accuracy about 0.60). The
specification's deferred items (OCR, DOCX, authentication, public hosting) are not built. What production-ready would take is in `docs/production_readiness.md`.
