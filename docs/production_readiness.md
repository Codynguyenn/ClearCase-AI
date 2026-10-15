# What production-ready would take

ClearCase is a **local portfolio MVP**: all six build phases and every deliverable of its specification are done (`docs/requirements_audit.md`),
and its limits are measured and stated. It is **not production-ready**, and this document says what that would take. It is grounded in what this
repository actually has and lacks, not a generic checklist. It is engineering planning, not a security audit and not legal advice; the effort
figures are my rough estimates for one engineer, not measurements. Every file and test it cites is checked by a test
(`backend/tests/test_requirements_audit.py`).

## What already helps (a foundation, not the finish line)

- Ownership is checked in every repository and every MCP tool, and the model never supplies a user id (`backend/app/repositories/review.py`,
  `backend/app/mcp_server/server.py`, `docs/decisions.md` 19).
- The assistant can only propose; export needs a short-lived, single-use authorization tied to the document hash and review revision
  (`docs/decisions.md` 20), and every export is re-read by an independent verifier (`docs/decisions.md` 15).
- Prompt injection is defended by structure, not by trusting the model (`docs/decisions.md` 22, `backend/tests/test_agent_injection.py`).
- Containers run as unprivileged users with health checks (`docker-compose.yml`); CI runs backend, frontend and browser jobs
  (`.github/workflows/ci.yml`); runs are bounded and cancellable (`backend/app/agent/runs.py`); secrets stay server-side (`.env.example`).

## Tier 1: blockers before any real person's data goes in

| Item | Why it is a blocker here | Today | What it takes | Rough effort |
| --- | --- | --- | --- | --- |
| **Real authentication and multi-user** | One server-controlled demo identity; `README.md` already says it is not authentication | A fixed `demo-user`; the MCP server takes its identity from its start-up environment (`docs/decisions.md` 7, 19) | Login (OIDC or similar), per-user identity, roles (reviewer, admin), identity passed into the MCP session per request, export authorization recorded against *who* approved | 1 to 2 weeks |
| **Safe handling of hostile PDFs** | A malicious PDF can hang or crash the parser, and the API process does the parsing | Extraction and export run inside the API process; the time budget is checked between pages and cannot interrupt a page that hangs inside PyMuPDF (`backend/app/services/pdf_processing.py`, `PROJECT_STATUS.md` known gaps) | Sandboxed, killable worker processes with memory and time limits and no network; malware scanning of uploads; keep PyMuPDF patched | 1 to 2 weeks |
| **Hosting security** | Everything is bound to localhost today | CORS for the dev origin; no TLS, rate limits or secrets manager (`backend/app/config.py`) | TLS everywhere (including to MongoDB), encryption at rest, a secrets manager instead of a `.env` file, rate limiting, security headers, then an external penetration test | 1 to 2 weeks plus the test |
| **Evaluation on real documents** | Every number so far is synthetic, easy and from one author | Two synthetic sets of 12 documents (`evaluation/README.md`) | Real, messy legal PDFs (scans, forms, tables, multi-column, layered text) with expert-reviewed ground truth, and the same measurements repeated | Depends on getting the documents |
| **Detection recall** | A redaction tool is only as good as what it finds | The NER model missed **10% to 33%** of person mentions across the two synthetic sets (`docs/decisions.md` 55, 56); phone and SSN patterns are US-only | A stronger or domain-tuned model, international formats and identifiers, and a minimum recall agreed in advance and checked on real data | Weeks, open-ended |
| **A red-team of the export** | The verifier checks text and rendering on fixtures, not adversarial files | `backend/tests/test_redaction.py`, `evaluation/pipeline_eval/REPORT_v2.md` | Try to recover redacted content (hidden layers, metadata, thumbnails, embedded files, font tricks) on real files, and fix what is found | 1 to 2 weeks |

## Tier 2: before a multi-user or public launch

| Item | Today | What it takes |
| --- | --- | --- |
| **A real job queue and workers** | Extraction, export and assistant runs live in the API process, so a restart loses in-flight work and a second instance cannot share it (`backend/app/agent/runs.py`) | A queue (Mongo-backed or a managed one), worker processes, and run cancellation that works across instances |
| **Observability and cost control** | Count-only logs; no metrics, tracing or alerts | Structured logs, metrics, traces, alerting, and per-tenant limits on hosted-model spend |
| **Database operations** | Indexes are created at start-up; no backups or restore drill | Versioned migrations, tested backups and restore, a retention policy, encryption at rest |
| **A vector index** | Brute-force cosine in Python (`backend/app/services/retrieval.py`), fine for thousands of passages | A real vector index behind the existing interface, and a re-evaluated cutoff |
| **OCR and more formats** | Scanned pages are flagged, not read; the specification deferred OCR and DOCX | OCR (with its own accuracy evaluation) and DOCX, or an explicit "text PDFs only" product line |
| **Load, fuzz and soak testing** | Unit, integration and one real-browser story | Large files, many users, malformed-PDF fuzzing, outage drills |
| **Supply chain** | `npm audit` clean; direct licences listed (`docs/dependency_licenses.md`) | Automated dependency and container scanning, an SBOM, and an audit of transitive licences (PyMuPDF is AGPL-3.0, which limits what can be offered commercially) |
| **Hosted-model data handling** | The chat tab says excerpts are sent and asks first (`frontend/src/components/ChatPanel.tsx`) | A signed data-processing agreement, zero-retention terms, an opt-out per customer, possibly a self-hosted model for sensitive clients |

## Tier 3: before it can honestly be sold

- **Legal and compliance review** (GDPR, CCPA or HIPAA depending on customers), liability wording, and review of the contract with the model
  provider. Without it the "no legal-compliance claim" caveat stays permanent.
- **A required human process.** A second reviewer and a full-document read-through. The verifier checks only what was approved: 31% of person
  mentions on one synthetic set were never proposed, and where no copy of the same text was approved they stay readable with no warning
  (`docs/decisions.md` 55).
- **Product gaps.** A one-click "propose the remaining copies" from the export warning (9 of 18 NER-based exports needed an acknowledgement on one set,
  4 of 18 on the other), collaborative review, undo history, a responsive layout, an accessibility (WCAG) audit, and e-discovery features such as
  redaction logs and Bates numbering if lawyers are the market.
- **Company basics.** Security and privacy policies, an incident runbook, service-level targets, support.

## Suggested order

1. Authentication and multi-user (everything else assumes it).
2. Sandboxed workers and a queue (they remove the hostile-PDF risk and make scaling possible).
3. Evaluation on real documents with an agreed recall target (it decides whether detection needs a better model).
4. A red-team of redaction recovery, then fixes.
5. Legal and privacy review, in parallel with the above because it depends on people, not code.

Items 1 to 4 and the Tier 2 basics are roughly **two to four months of focused work for one engineer** (an estimate). Real-data collection and legal
review depend on people outside engineering.

## A definition of "ready" that can be checked

Production-ready would mean, at minimum: authenticated multi-user access with no cross-user reads in a dedicated test; extraction and export in
sandboxed workers with limits; an external penetration test with no open high findings; a recall target met on a real, expert-labelled set and
re-checked in CI on a fixed sample; a red-team that recovers nothing from redacted exports; a restore drill that works; and a signed review by someone
qualified to judge the legal claims. None of those is true today, and this repository does not claim otherwise.
