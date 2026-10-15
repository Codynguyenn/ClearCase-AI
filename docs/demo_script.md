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
| 1:05 | Reject the first finding (a "Maria Delgado-Reyes"). Set the status filter to *proposed*, click **Approve shown**. | "I decide. I rejected this one and approved the rest. The spaCy model is pretrained and not perfect, which is exactly why a human reviews." |
| 1:30 | **Chat** tab. Click the `summary` chip, send. Open **What it did**. | "This is the assistant. In this offline mode it is a labelled script, not an AI. It calls the same tools a live model would, over MCP, and shows every tool call." |
| 1:45 | Send `policy can company names stay visible?`. Click the citation chip. Press Escape. | "It quotes a sentence from a policy and cites it. The citation is checked by code: the passage was really retrieved and the quote really appears in it. It shows what the policy says; it does not decide." |
| 2:10 | Send `export`. It refuses. Click **Authorize export**, then **Ask the assistant to export now**. | "The assistant cannot export on its own. Only I can arm it, for five minutes, once. Even then it exports only what I approved." |
| 2:30 | **Export** tab: show **verified** and its checks; click **Download**. | "Export really removes the text, then a separate check re-reads the file and looks for the approved text and for black boxes. If a required check fails, there is no download." |
| 2:45 | Close with the numbers. | "On 12 synthetic documents, with the rules written first, a model-assisted workflow scored F1 about 0.98 against 0.82 for the detectors alone, and the cheapest model matched the dearest at about a twenty-seventh of the cost. That is synthetic and easy; I do not claim it for real documents." |

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
