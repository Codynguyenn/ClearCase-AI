# Second held-out retrieval set: protocol (pre-registered)

Written **before any question of this set exists** and before the retriever has seen any of it. If anything here has to change after
generation starts, the change goes in the incident log at the bottom with the reason, and the report must say so.

Everything is **synthetic**: two fictional policies, and questions written and labelled by language models. Nothing here is the
accuracy of the system on real users' questions.

## 1. Why a second set

The first held-out set (`retrieval_heldout.json`, 44 questions) was written by the same author as the policies, is small, and has used
its two allowed runs. It measured a balanced accuracy of 0.56 for the similarity cutoff (0.38) against 0.84 on the development set the
cutoff was tuned on. One small same-author set cannot say whether that was typical. This set is **larger and more independent**: its
questions are written by a model that was never shown the policies, and labelled by two other model runs.

It is used to **measure** the production retriever once. It is never used to tune anything.

## 2. What is measured

The production `MongoCosineRetriever`, unchanged, against a scratch database seeded with the two sample policies (19 passages,
version 1.0): cosine similarity with `all-MiniLM-L6-v2` (pinned revision), cutoff 0.38, keyword weight 0.15, the top 3 passages shown,
and the reviewer-only weak tier (0.30 to 0.38) reported separately. The runner is `evaluation/eval_heldout.py --set 2`; the metrics are
the same as for the first set (hit@1, hit@3, MRR, decisions at the cutoff, balanced accuracy, margin), each with a 95% Wilson interval.

## 3. How the questions are made

1. **Writer** (`claude-haiku-5-5`, no policy text): six calls, one per persona, 25 questions each (150 candidates).
   It is told only that the library has one document about redaction and one about sharing and keeping records. About two thirds of
   what it is asked for should be plausibly settled by such documents, one third about nearby topics they might not cover.
2. **De-duplication** (free): drop empty or over-long questions, repeats inside the list (word overlap of 0.7 or more), and near-copies of any
   question in the development or first held-out set (word overlap of 0.6 or more). Counts are reported.
3. **Labelers** (`claude-sonnet-5-5` and `claude-haiku-5-5`, each shown all 19 passages with their section names): for each question,
   in a different random order per labeler, give a verdict (A answerable, N not addressed, U unclear) and the passage numbers that
   settle it. Batches of 50; effort `low`.
4. **Selection** (free, by code, no manual edits): keep a question only if both labelers gave the same definite verdict (A or N). An
   answerable question's relevant passages are the ones **both** labelers named (the intersection); if they share none, the question is
   dropped. Every exclusion is counted by reason and reported (missing label, unclear, verdicts differ, different passages).
5. **Freeze**: the kept questions are written to `evaluation/retrieval_heldout2.json` and their SHA-256 to `retrieval_heldout2.sha256`.
   The runner refuses a changed file.

No top-up round, no manual edits to any question or label, no second draw. The set is whatever this process yields. If a class ends up
with fewer than 15 questions, the report says the set cannot support conclusions about that class.

## 4. The prompts (exact text; the build script refuses to spend unless this file contains their hashes)

Writer prompt sha256: `90d332784aa57efda6381aa9407edf82a862897f766015da8eb34794eb28b8d7`

> You write realistic test questions for an internal policy assistant. You have not seen the company's policy documents and must not guess or imitate their wording; write the questions real people would actually type.
>
> Setting: a small data-analytics company keeps an internal policy library. It has two documents: one about how documents are redacted (hiding personal information before a document is shared), and one about sharing documents outside the company and retaining them. Staff use a redaction tool and ask a policy assistant questions about these policies.
>
> Write {n} different questions that {persona} might ask while working with contracts, offer letters, invoices, NDAs, leases or demand letters.
>
> Mix these kinds, in no particular order:
> - about two thirds are questions about redacting, sharing or retaining documents that the two documents above might plausibly settle;
> - about one third are about nearby topics that such a library might NOT cover (for example employment law, IT security, privacy law in other countries, billing, contract drafting, litigation strategy, tool settings, document formats).
>
> Vary the form: some very short, some a full sentence, some describing a concrete situation first. Do not label the kinds, do not explain, and do not repeat a question in different words. Do not start more than a few questions with the same word.
>
> Output only a JSON array of {n} strings.

Labeler prompt sha256: `8ffe5e481e17868d1776f012754094b75059de3cff059d664399ed33a3b9b8e0`

> You are a careful reviewer labelling test questions against a fixed set of policy passages. Use only the passages you are given; do not use outside knowledge about what such policies usually say.
>
> Policy passages (each is numbered; the number is its id):
>
> {passages}
>
> For each question below, decide:
> - "A": answerable. One or more passages contain a statement that directly settles the question (a rule, requirement, permission or prohibition that an assistant could quote as the answer). List the numbers of the passages that contain it (at most three).
> - "N": not addressed. The passages do not settle the question: the topic is absent, only loosely related, or a passage mentions it only to say the policy does not cover it. Use [] for the numbers.
> - "U": unclear. The passages partly address it, or the answer depends on facts the question does not give, so careful readers could disagree. List the best-matching passage numbers.
>
> Be strict about "A": a passage that is merely on the same topic is not enough.
>
> Questions:
> {questions}
>
> Output only a JSON array with one object per question, in order, like:
> [{{"id": "c001", "v": "A", "p": [3]}}, {{"id": "c002", "v": "N", "p": []}}]

The writer personas are: a first-year paralegal reviewing contracts for a client; a senior associate preparing documents to send to opposing counsel; a client-service manager at a small analytics company; an IT and security analyst who supports the legal team; an HR coordinator who handles offer letters and employee files; a compliance officer answering a request from a regulator.

## 5. Running it

`backend/.venv/bin/python evaluation/eval_heldout.py --set 2 --label baseline`, **once**. No other label, no repeat, no configuration
change before or after. Per-question results are saved but are not used to change anything. A bug found in the runner is fixed and
logged below; the measurement is repeated only if the bug affected it, and both runs are reported.

## 6. Predictions (written before any result)

1. Ranking is worse than on the first held-out set: hit@3 below 0.75 (questions written blind to the policies share less wording with them).
2. The cutoff still cannot separate answerable from unanswerable: balanced accuracy below 0.70.
3. At least a quarter of the answerable questions are wrongly told "no matching policy".
4. At least one unanswerable question has a best cosine of 0.38 or more (the margin to the best unanswerable is negative).
5. Pooling both held-out sets, the upper 95% bound for hit@3 is below 0.90.

## 7. How the result will be read (decided now)

- The headline for any claim about the cutoff is the **pooled** estimate over both held-out sets (the first set's `final` run and this
  one), with its interval, next to each set on its own.
- If this set disagrees with the first (for example a balanced accuracy of 0.75 or more), the report says so plainly and says the
  difference is within what small samples and different authors produce; the favourable number is not chosen.
- Secondary, fixed now: for unanswerable questions, how many were shown only the "Topics this policy does not address" passage (it
  names topics on purpose, so it is easy to match); this is reported separately and does not change the main number.
- Nothing is tuned on this set. If a better method (for example a re-ranker or a relevance check) is ever tried, it needs a **third**
  set; this one is then a development set and must be said to be.

## 8. Spending

Hard cap $0.20; the build script stops before any call that could pass it (realistic estimate, then actual cost) and
logs tokens and cost per call in `raw/`. Expected about $0.06 to $0.08 at list prices. Only the synthetic policy text and the generated
questions are sent to the provider.

## 9. Known limits (repeated in the report)

Questions and labels are produced by language models, not people, and by the same family as the assistant under study. The writer was
told what the two documents are about. The second labeler is the writer's model, so it is a consistency check, not an independent
expert. Keeping only questions both labelers agree on removes the ambiguous ones, so the set is likely **easier** than real use.
The labelers see passages, not whole documents. Two fictional policies and 19 passages. The author of the prompts and of the
policies is the same assistant. This is not real users' questions.

## Incident log

| When | What happened | Effect |
| --- | --- | --- |
| before any call | A test that checks the writer prompt is blind to the policies found that it said "keeping records", the name of a policy section (a generic description, but the rule is no section names). | Reworded to "retaining them"; the writer prompt hash in this file was updated before any call was made. |
| before any call | The first plan used Opus as second labeler and a rationale field: estimated about $0.33, over the cap and not "a few cents". | Design changed before anything was spent: Haiku as second labeler, no rationale field, cap $0.20. |
