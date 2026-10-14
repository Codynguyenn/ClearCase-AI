"""Retrieve the policy passages relevant to a question.

How it works
------------
1. **Filter first.** Only chunks that belong to this owner, to an *active* policy
   version, and that were embedded with the *current* model revision are
   candidates. The filtering happens in the database query, before any scoring.
2. **Score.** The question is embedded and compared with every candidate by cosine
   similarity (a dot product, because vectors are unit length).
3. **Gate.** A passage is shown only if its cosine similarity reaches
   ``retrieval_min_score``. That number is a similarity *heuristic* chosen by
   evaluation (see ``evaluation/tune_retrieval.py``); it is not a probability.
4. **Order.** Passages that passed the gate are ordered by cosine plus a small
   keyword-overlap bonus (measured to help on the dev set), so exact policy terms
   break near-ties. The reported ``score`` is always the plain cosine.
5. **Say so when there is nothing.** If no passage passes the gate, the result is
   ``no_relevant_policy``; it never returns the "least bad" passage as if it were a
   rule. If embeddings cannot be produced, the result is ``unavailable``; keyword
   search is **not** used as a stand-in and relabelled as semantic search.

Scale limit
-----------
Scoring is brute force in Python over vectors loaded from MongoDB. That is fine
for hundreds to a few thousand chunks (one policy library for one user). For tens
of thousands of chunks or many users, replace ``MongoCosineRetriever`` with an
implementation backed by a vector index; callers only depend on ``PolicyRetriever``.
"""

from __future__ import annotations

import re
from typing import Protocol

import numpy as np

from app.config import Settings
from app.db import Mongo
from app.models.policies import (
    PolicyStatus,
    RetrievalMethod,
    RetrievalResult,
    RetrievalStatus,
    RetrievedPassage,
)
from app.repositories.policies import PolicyChunkRepository, PolicyRepository, policy_key
from app.services.embeddings import EmbeddingUnavailable, embedding_status
from app.services.policy_service import EmbedderProvider, passage_from_docs

MAX_QUERY_CHARS = 500
WEAK_PASSAGE_LIMIT = 3
_STOP = set(
    "the and for with that this what when how are does can should about have from your you our any not but they them there their".split()
)


def _terms(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _STOP}


class PolicyRetriever(Protocol):
    """The seam where a production vector index could replace brute-force cosine."""

    def retrieve(self, owner_id: str, query: str, *, top_k: int = 3) -> RetrievalResult: ...


class MongoCosineRetriever:
    def __init__(self, mongo: Mongo, settings: Settings, embedder_provider: EmbedderProvider | None = None) -> None:
        from app.services.embeddings import get_embedder

        self._settings = settings
        self._policies = PolicyRepository(mongo.db)
        self._chunks = PolicyChunkRepository(mongo.db)
        # With no injected embedder (the normal case) we use the real local model and check it is installed.
        self._uses_real_model = embedder_provider is None
        self._embedder_provider = embedder_provider or (lambda: get_embedder(settings))

    def _method(self) -> RetrievalMethod:
        s = self._settings
        weak = s.retrieval_weak_min_score if s.retrieval_weak_min_score < s.retrieval_min_score else None
        return RetrievalMethod(
            model=s.embedding_model, revision=s.embedding_revision, min_score=s.retrieval_min_score, weak_min_score=weak
        )

    def _result(self, status: RetrievalStatus, message: str, query: str, **extra) -> RetrievalResult:
        return RetrievalResult(status=status, message=message, query=query, passages=[], method=self._method(), **extra)

    def retrieve(self, owner_id: str, query: str, *, top_k: int = 3) -> RetrievalResult:
        settings = self._settings
        query = " ".join(query.split())[:MAX_QUERY_CHARS]
        top_k = max(1, min(top_k, settings.retrieval_top_k_max))
        if not query:
            return self._result(RetrievalStatus.NO_RELEVANT_POLICY, "The question was empty.", query)

        # An injected embedder (tests) is used as-is; otherwise check the real model first.
        status = embedding_status(settings) if self._uses_real_model else None
        if status is not None and not status.available:
            reason = status.reason or "Semantic retrieval is unavailable."
            return self._result(
                RetrievalStatus.UNAVAILABLE,
                f"Semantic policy search is unavailable: {reason} No policy guidance was retrieved, "
                "and keyword search is not used as a substitute.",
                query,
            )

        keys = self._policies.active_keys(owner_id)
        if not keys:
            return self._result(
                RetrievalStatus.NO_POLICIES, "There are no active policies to search. Upload or seed a policy first.", query
            )

        try:
            embedder = self._embedder_provider()
            query_vector = np.array(embedder.embed([query])[0], dtype=np.float32)
        except EmbeddingUnavailable as exc:
            return self._result(
                RetrievalStatus.UNAVAILABLE,
                f"Semantic policy search is unavailable: {exc} No policy guidance was retrieved.",
                query,
            )

        revision = embedder.revision
        candidates = self._chunks.candidates(owner_id, keys, revision)
        stale = self._chunks.count_stale(owner_id, keys, revision)
        if not candidates:
            detail = (
                "The active policies were indexed with a different embedding model revision. Re-index them."
                if stale
                else "The active policies have not been embedded yet. Re-index them."
            )
            return self._result(
                RetrievalStatus.UNAVAILABLE,
                f"Semantic policy search is unavailable: {detail} No policy guidance was retrieved.",
                query,
                chunks_stale_ignored=stale,
            )

        matrix = np.array([c["embedding"] for c in candidates], dtype=np.float32)
        cosine = matrix @ query_vector
        query_terms = _terms(query)
        keyword = np.array(
            [
                len(query_terms & _terms(c["section"] + " " + c["text"])) / len(query_terms) if query_terms else 0.0
                for c in candidates
            ],
            dtype=np.float32,
        )
        ordering = cosine + settings.retrieval_keyword_weight * keyword

        passing = [i for i in np.argsort(-ordering, kind="stable") if cosine[i] >= settings.retrieval_min_score]
        best = float(cosine.max())
        parents = {policy_key(d["policy_id"], d["version"]): d for d in self._policies.list(owner_id) if d["status"] == PolicyStatus.ACTIVE.value}
        if not passing:
            # Nothing reached the cutoff. The reviewer (not the assistant) may still be offered a few weak matches.
            weak: list[RetrievedPassage] = []
            if settings.retrieval_weak_min_score < settings.retrieval_min_score:
                near = [
                    i
                    for i in np.argsort(-ordering, kind="stable")
                    if settings.retrieval_weak_min_score <= cosine[i] < settings.retrieval_min_score
                ][:WEAK_PASSAGE_LIMIT]
                for rank, i in enumerate(near, start=1):
                    parent = parents.get(candidates[i]["policy_key"])
                    if parent is not None:
                        base = passage_from_docs(candidates[i], parent)
                        weak.append(RetrievedPassage(**base.model_dump(), score=round(float(cosine[i]), 4), rank=rank))
            return self._result(
                RetrievalStatus.NO_RELEVANT_POLICY,
                "No policy passage was similar enough to this question. That is a similarity heuristic, not proof "
                "that the policy is silent. Do not assume a rule either way: tell the user that no matching passage "
                "was found and refer the question to the reviewer.",
                query,
                chunks_considered=len(candidates),
                chunks_stale_ignored=stale,
                top_score_below_cutoff=round(best, 4),
                weak_passages=weak,
            )

        passages: list[RetrievedPassage] = []
        for rank, i in enumerate(passing[:top_k], start=1):
            chunk = candidates[i]
            parent = parents.get(chunk["policy_key"])
            if parent is None:
                continue
            base = passage_from_docs(chunk, parent)
            passages.append(RetrievedPassage(**base.model_dump(), score=round(float(cosine[i]), 4), rank=rank))
        return RetrievalResult(
            status=RetrievalStatus.OK,
            message=f"{len(passages)} passage(s) from the active policies. Cite them by citation_id; do not state rules they do not contain.",
            query=query,
            passages=passages,
            method=self._method(),
            chunks_considered=len(candidates),
            chunks_stale_ignored=stale,
        )
