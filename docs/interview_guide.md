# Interview guide

How to explain ClearCase honestly in an interview. Every claim below is backed by something in the repository (decision
numbers refer to `docs/decisions.md`). When you are unsure, say what was measured, how, and what it does not show: that is
the strongest thing about this project.

## The 30-second version

"ClearCase is a local web app for redacting legal PDFs. Tools **propose** what is sensitive, a human **decides**, and the export
is checked by a separate verifier. It has a real MCP tool server, retrieval over redaction policies with checked citations, and a
chat assistant that can only propose. I measured the assistant on synthetic documents with the rules written before the run,
found a flaw in my own labels, and reported it."

## The architecture in one minute

React front end, FastAPI back end, MongoDB (with GridFS for the PDF files), and a **separate MCP server process** that the
assistant talks to over stdio. Layers: API routes → services → repositories (every query filtered by owner) → models. The PDF
engine is PyMuPDF; detection is validated patterns plus a pretrained spaCy model; embeddings come from a pretrained
sentence model run locally. The diagram is in `docs/architecture.md`.

## The six topics, each as: what it is here, what to say, what they may push on

### MCP (Model Context Protocol) — decisions 17-20
- **Here:** seven tools (search, excerpt, detect, policy lookup, propose, review summary, export) served by `python -m app.mcp_server`
  over JSON-RPC on stdio. The backend starts it, lists the tools with their JSON schemas, and calls them through the protocol.
- **Say:** "The point of a separate process is the boundary. The model can do only what the tool list allows, arguments are
  validated against schemas, and the server enforces its safety rules no matter who calls."
- **Pushed on "why not plain function calls?"** They would skip the boundary; and the project spec forbids calling direct calls MCP.
- **Pushed on "how is the document access limited?"** Identity comes from the process (set at start-up), never from a tool
  argument, and the document id is pinned by the loop for tools that take one. A model cannot name another document or user.
- **Pushed on "can the assistant export?"** Only after a human clicks "Authorize export": a 5-minute, single-use token tied to the
  document hash, the review revision and the approved rectangles. The assistant never sees it. (Decision 20; the browser test shows it.)

### RAG (retrieval-augmented generation) — decisions 24-31, 36-39
- **Here:** policies are split into passages with stable citation ids (`policy:version:n`), embedded locally
  (`all-MiniLM-L6-v2`, pinned revision), stored in MongoDB, and searched by cosine similarity (brute force; fine for thousands).
- **Say:** "I did not let the model free-write citations. A quoted sentence is verified by code: the passage must have been retrieved
  in that request and the quote must appear in it word for word. That proves the quote is real, not that the model understood it."
- **Honest numbers (decision 36):** on a frozen held-out set of 44 questions, the right passage was ranked first for 18/28 answerable
  questions (0.64) and in the top 3 for 21/28 (0.75). The similarity cutoff (0.38) was tuned on a dev set and **did not hold up**:
  balanced accuracy 0.56 on held-out. So the assistant is built not to rely on it: it must quote or say the policies do not address it.
- **Pushed on "why is that cutoff weak?"** A cosine score is a similarity heuristic, not a probability. A better embedding model ranked
  better but did not decide better (decision 37).
- **A mistake worth telling (decision 38):** I explained a rule in a Python docstring, and Pydantic turned it into the JSON schema sent
  to the model, so my comment was leaking a reviewer-only rule to the assistant. A test caught it.

### NoSQL / MongoDB — decisions 2, 3, 9
- **Here:** documents, page text, findings, exports, audit events, policies and chunks with vectors, conversations, runs and tool events
  are collections; the PDF bytes are in GridFS. Synchronous `pymongo`.
- **Say:** "Document-shaped data with variable parts (per-page word boxes, evidence lists, citations) fits documents well. Integrity
  rules I care about are enforced by the database: partial unique indexes make 'one active run per conversation' and 'one message per
  client id' true even under concurrent requests."
- **Pushed on "no joins, no transactions?"** Right: the code uses no multi-document transactions. Every collection is keyed by owner and
  document; the rules that must hold under concurrency are unique indexes (one page per document and number, one chunk per citation id,
  one message per conversation counter, one active run per conversation). Multi-step updates are ordered so a failure in the middle leaves
  the safe state: a bulk decision bumps the review revision *before* it changes statuses, so a crash can only invalidate export
  authorizations, never leave one valid for a review that changed. I would not pick Mongo for heavy relational reporting.
- **Tests use a real MongoDB, not a mock** (decision 9): mocks cannot show index behaviour.

### Entity detection — decisions 4, 13, 14
- **Here:** patterns for e-mail, US phone, SSN-shaped numbers and address lines; spaCy NER (pretrained, English) for people, organisations,
  places. Person and organisation are separate categories so "keep company names" works.
- **Say:** "I did not train a model; spaCy's is pretrained and imperfect, and it has no honest confidence score, so I never show one."
  Detectors only propose; the **server derives rectangles** from the stored word map, so no model ever supplies coordinates.
- **Honest numbers (decision 51):** on 12 synthetic documents the pattern detectors alone scored recall 0.45, F1 0.62; adding NER gave
  recall 0.74, F1 0.82; the model-assisted workflow scored F1 about 0.98. Synthetic, easy, one author.

### Redaction correctness — decisions 15, 5
- **Here:** export is authorize → redact (PyMuPDF `apply_redactions`: text really deleted, image pixels blanked) → scrub metadata →
  **independently verify** (re-extract text; look for approved text and for black boxes) → store. A failed required check means no download.
- **Say:** "Drawing a black box over text is not redaction; the text is still in the file. I delete it and then prove it by re-reading
  the output." The browser test goes one step further: it uploads the downloaded file back and searches it.
- **Pushed on "what does it not catch?"** Text in images or annotations; scanned pages (no OCR: they are flagged as unsupported and
  need a human acknowledgement); anything the detectors and the reviewer both miss; the file's other copies. It never claims compliance.

### Design trade-offs (pick two or three)
- **Demo mode vs live mode:** the default is a labelled scripted assistant, so the app and CI work without a key or cost; live mode
  never silently falls back to simulated answers.
- **Propose-only assistant:** it cannot approve, reject, authorize or read other documents, so a misbehaving or injected model has a
  small blast radius (tests in `tests/test_agent_injection.py` do not depend on the model behaving).
- **Polling instead of streaming:** replies appear when a request finishes; simpler and robust, at the cost of no token streaming.
- **Local single-user MVP:** the demo identity is not authentication. Multi-user needs real auth and a worker process.

## The evaluation story (the part to be proud of) — decision 51

1. Wrote the **protocol first**: systems, scoring rules, six predictions, a decision rule, a spending cap.
2. Froze a 12-document held-out set by SHA-256; the runner refuses a changed file.
3. Ran each of 3 models once per document (36 runs, $2.78). Results: F1 0.988 / 0.981 / 0.981 (Haiku / Sonnet / Opus) vs 0.823 for the
   detectors; the models were not distinguishable, Opus cost about 27 times Haiku per document. 5 of 6 predictions held.
4. **Found my own labelling flaw by reading the misses**: all 16 were a company's address, which my labels called sensitive although the
   request asks for individuals' information. I kept the headline as frozen, and added a clearly marked post-hoc table.
5. What I would say: "It supports a narrow claim, that the model-assisted workflow found more than the detectors alone on easy synthetic
   documents and the cheapest model did as well. It does not predict real-world accuracy."

## Questions to expect, with straight answers

| Question | Answer |
| --- | --- |
| Did you train a model? | No. Pretrained spaCy and a pretrained embedding model; the language model is a hosted API. |
| How do you know it works? | Backend tests against a real MongoDB (563), component tests (94), a real-browser test (5), plus the pre-registered evaluation. Each says what it covers; none says it works on real documents. |
| How do you test the tests? | I break the code on purpose and check the tests fail. Several tests passed for the wrong reason until I did (see decisions 50-52). |
| What if the model follows an injected instruction in the PDF? | It still cannot approve, cannot export without the reviewer's authorization, and cannot read other documents. Injection is defended by structure, not by trusting the model (decision 22). |
| Is it secure? | It is a local demo: the identity is not authentication, and live mode sends excerpts to a provider (the UI says so and asks first). Keys live only in the git-ignored `.env`; the SDK exception is not chained, so a key cannot reach logs or the database. |
| What was the hardest bug? | Pick one: the policy tool failing in live mode because the loop injected a `document_id` it does not take (found only by a real-server test); or the docstring that leaked a rule into the model's schema. |
| What would you do next? | A harder, longer evaluation set with labels that separate personal from business addresses, written first; real authentication and a worker process; OCR for scanned pages; fix bulk "Approve shown" overwriting earlier decisions. |

## Numbers you can quote (and where they come from)

| Claim | Number | Source |
| --- | --- | --- |
| Assistant, detectors only (pattern + NER) | recall 0.744, F1 0.823 | `evaluation/assistant_eval/REPORT_heldout.md` |
| Assistant, Haiku / Sonnet / Opus | F1 0.988 / 0.981 / 0.981 | same |
| Cost per document | $0.006 / $0.074 / $0.152 | same (list prices, from token counts) |
| Retrieval, right passage first / top 3 | 18/28 = 0.64 / 21/28 = 0.75 | `evaluation/README.md` (held-out, 28 answerable questions) |
| Retrieval cutoff balanced accuracy | 0.84 dev, **0.56** held-out | decision 30 |
| Tests | 563 backend, 94 frontend, 5 browser | `PROJECT_STATUS.md` |

## Plain-English glossary

- **Held-out set:** examples you do not look at while building, so the score is not flattered.
- **Pre-registered:** the rules and predictions are written down before the result exists, so they cannot be bent afterwards.
- **F1:** one number mixing precision (of what I flagged, how much was right) and recall (of what was there, how much I found).
- **Cosine similarity:** how closely two vectors point the same way; here, how similar a question and a passage are.
- **Idempotent:** doing it twice has the same effect as once (a retried export returns the same export).
- **Prompt injection:** text inside a document that tries to give the assistant orders.
