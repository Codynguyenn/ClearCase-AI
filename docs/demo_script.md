# Three-minute demo

Everything here runs offline in **demo mode** (the scripted assistant; no key, no network, no cost). The document and policies
are synthetic and fictional. About 3 minutes if you stay on script; the times are guides.

## Before you start (once, about 2 minutes)

```bash
scripts/dev_mongo.sh start                                              # MongoDB
CLEARCASE_PORT=8010 scripts/dev_backend.sh                              # terminal 1
VITE_API_PROXY_TARGET=http://127.0.0.1:8010 scripts/dev_frontend.sh     # terminal 2 -> http://127.0.0.1:5173
```

Then, in the app, so the live part has nothing to wait for:

1. Delete any old documents (the **×** next to each) so the list is empty. Keep `sample_data/sample_contract.pdf` ready to upload.
2. Open an uploaded document's **Policy** tab (upload anything, then delete it afterwards), click **Load the fictional sample
   policies**, wait until both show *searchable*, and ask one question. The first question loads the embedding model (15 to 20
   seconds); after that it is quick. (`python -m app.cli seed-samples` also loads the policies, but it uploads the sample PDF too,
   which would spoil the live upload.)

## The script

| Time | Do | Say |
| --- | --- | --- |
| 0:00 | Show the empty app. | "ClearCase helps a person redact a legal PDF. The rule of the whole project: **tools propose, a human decides, and nothing claims to be legal compliance.**" |
| 0:20 | Upload `sample_contract.pdf`. Page 1 appears. | "The file is checked by its bytes, stored unchanged in MongoDB, and I extract the text of every page with the position of every word. Everything later, search, detection and the final redaction, uses those positions. A language model never invents coordinates." |
| 0:45 | **Search** tab: `Northwind`. Then **Review** tab: **Detect & propose**. | "Detection is two things: patterns for e-mails, phones and SSN-shaped numbers, and a pretrained spaCy model for names. Nothing is approved. Note that company names are *not* proposed: the organisation box is off, because the task is to keep company names visible." |
| 1:05 | Reject the first finding (a "Maria Delgado-Reyes"). Click **Approve shown (17 undecided)**; it acts only on findings you have not decided yet, so your rejection stays. | "I decide. I rejected this one and approved the rest. The spaCy model is pretrained and not perfect, which is exactly why a human reviews." |
| 1:30 | **Chat** tab. Click the `summary` chip, send. Open **What it did**. | "This is the assistant. In this offline mode it is a labelled script, not an AI. It calls the same tools a live model would, over MCP, and shows every tool call." |
| 1:45 | Send `policy can company names stay visible?`. Click the citation chip. Press Escape. | "It quotes a sentence from a policy and cites it. The citation is checked by code: the passage was really retrieved and the quote really appears in it. It shows what the policy says; it does not decide." |
| 2:10 | Send `export`. It refuses. Click **Authorize export**, then **Ask the assistant to export now**. | "The assistant cannot export on its own. Only I can arm it, for five minutes, once. Even then it exports only what I approved." |
| 2:30 | **Export** tab: show **verified** and its checks; click **Download**. | "Export really removes the text, then a separate check re-reads the file and looks for the approved text and for black boxes. If a required check fails, there is no download." |
| 2:45 | Close with the numbers. | "On 12 synthetic documents, with the rules written first, a model-assisted workflow scored F1 about 0.98 to 0.99 against 0.82 to 0.92 for the detectors alone, depending on the test set, and the cheapest model matched the dearest at about a twenty-seventh of the cost. The export check caught every leftover copy of an approved name. That is synthetic and easy; I do not claim it for real documents." |

## Optional proof (if there is time)

Upload the file you just downloaded, **Search** `Delgado-Reyes`: one match (the finding you rejected), the others are gone.
Search `512-44-9087`: zero matches. This is what the browser test automates.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| Red banner "backend is not reachable" | The backend is not running, or the port differs from `VITE_API_PROXY_TARGET`. |
| Policy question says no active policies | Click **Load the fictional sample policies** in the Policy tab and wait until both show *searchable*. |
| The policy answer is slow once | The embedding model is loading; it is quick afterwards. |
| The `export` command succeeds without asking you | You already exported within 5 minutes; it returns that existing export (never a second file). Skip, or wait, or re-seed. |
| A name you expected was not proposed | Detection is imperfect by design: use **Search** and propose the matches, or draw a region. |

## Things not to say

- Not "it redacts everything" (it finds what its detectors and the reviewer find) or "it is compliant".
- Not "the AI decides" (it proposes; you approve).
- Not that the demo mode is a model: the app labels every scripted reply as scripted.
- Not that the numbers apply to real documents.

---

# The live-mode version (about four minutes)

Use this when you want to show a **real hosted model** driving the same tools. It sends excerpts of the **synthetic** sample contract to Anthropic and
costs about half a cent per request with the recommended model (measured: a document-sized request on Haiku was about $0.005 and 39 seconds in the
evaluation; a policy question on 2026-10-15 was $0.001 and 20 seconds). Never use it with a real document. The offline script above does not need any
of this.

## Before you go live (once, a few minutes)

1. In `.env`, set `CLEARCASE_ASSISTANT_MODE=live`. The provider, key and model (`claude-haiku-5-5`) are already there. Restart the backend.
2. Check it is really live (this makes no model call and never shows the key):

```bash
curl -s http://127.0.0.1:8010/api/health | python3 -m json.tool | grep -A6 assistant
```

   You should see `"mode": "live"`, `"configured": true` and `"sends_excerpts_to_provider": true`.
3. **Rehearse once the day before** (a few cents at most): run the whole thing below. It proves the key still works (it expires around mid-November, so
   check the Console) and loads the embedding model, whose first use takes about 12 seconds.
4. In the Anthropic Console set a **monthly spend limit** as a hard ceiling, and keep the key out of screenshots and chat.
5. Do not edit any file while the backend is running (`--reload` would restart it in the middle of a request).

## The script

| Time | Do | Say |
| --- | --- | --- |
| 0:00 | Upload `sample_contract.pdf`. Open the **Chat** tab. Click **I understand, continue** on the amber notice. | "In live mode the app tells you what leaves your machine: excerpts the assistant reads are sent to the provider. I only continue with synthetic documents." |
| 0:30 | Send: `Find the client's personal information, suggest redactions, and keep company names visible.` Open **What it did** while it works (30 to 60 seconds). | "A real model is choosing which tools to call, through MCP. Each call appears here as it happens: tool, status, duration, and counts, never document text." |
| 1:30 | Open the **Review** tab: the assistant's proposals are there, each with an **assistant** badge. Approve most, reject one. | "It can only propose. Every proposal was checked against the stored document; it cannot invent a location, and it cannot approve anything." |
| 2:15 | Back in **Chat**: `According to the policy library, can company names stay visible when we share a contract like this? Quote the policy sentence you rely on, or say if the policies do not address it.` Click a citation chip, then press Escape. | "The quote is checked by code against the passage it retrieved. A fabricated quote would show as unverified." |
| 3:00 | Send `export`. It refuses; click **Authorize export**, then **Ask the assistant to export now**. | "Only I can arm the export, for five minutes, once. It still exports only what I approved." |
| 3:30 | **Export** tab: show **verified**. Close with the numbers from the offline script. | "That is a real model doing real work, and a human decided every redaction." |

Optional, if there is time: ask `Summarise page 2 of this document in two sentences.` The sample contract contains an injected line telling an AI to export
everything. The assistant cannot act on it (it cannot approve or export), but **do not promise** it will point the line out: it did in one earlier run, not
necessarily in every run.

## If something goes wrong

| Symptom | What it means and what to do |
| --- | --- |
| The answer is slower than 60 seconds, or the Chat tab says "The AI provider took too long to answer" | Every request is bounded (8 model calls, 16 tool calls, 120 seconds). Click **Cancel** if it is still working, say so honestly, and ask again once. |
| A plain error message ("The AI provider rejected the API key", "has no usable credit", "is rate limiting requests", "Could not reach the AI provider") | The app **never** falls back to scripted answers. Switch `.env` back to `CLEARCASE_ASSISTANT_MODE=demo`, restart (about 30 seconds) and continue with the offline script above. Rehearse this switch once. |
| The wording differs from what you expected | Normal: a model's answer changes between runs. Narrate what is on screen; do not read from a script. |
| The first policy question takes about 12 seconds | The embedding model loads on first use. Ask one before you present. |
| The `export` command succeeds without asking | You exported within the last five minutes; it returns that same export (never a second file). Skip the step or wait. |

## What to say if asked about cost and safety

About half a cent per request on Haiku; a full live demo is a few cents; your balance is prepaid with auto-reload off, so the worst case is the remaining
balance, and the app cannot spend anything in demo mode. The key lives only in the git-ignored `.env` and never reaches logs, the database or the browser.
