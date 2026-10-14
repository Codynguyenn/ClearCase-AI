"""Local text embeddings for policy retrieval.

What an embedding is (plain English)
------------------------------------
An embedding model turns a sentence into a list of numbers (here 384 of them) so
that sentences with similar *meaning* end up with similar numbers. "Should I hide
the client's name?" lands near "Personal names of clients must be redacted" even
though they share almost no words. Retrieval then just compares those lists.

Rules this module enforces
--------------------------
* **Pretrained, local, pinned.** We use a pretrained sentence-transformers model
  (we did not train it) and pin the exact model *revision* so stored vectors can be
  traced to the model that produced them.
* **No surprise downloads.** The model is fetched once by ``download_embedding_model``
  (``scripts/download_models.sh``). At runtime it is loaded strictly from the local
  cache (``local_files_only``); if it is not there, we report "unavailable" instead
  of reaching for the network or pretending something else is semantic search.
* **No remote code.** ``trust_remote_code`` is always False.
* **Honest status.** ``embedding_status`` says whether semantic retrieval can run
  and why not. Callers must not substitute keyword search and call it semantic.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    """Semantic embeddings cannot be produced right now (reason is user-readable)."""


class EmbeddingStatus(BaseModel):
    available: bool
    enabled: bool
    model: str
    revision: str
    dimension: int | None = None
    reason: str | None = None


class Embedder(Protocol):
    """What the rest of the app needs from an embedding model."""

    model_id: str
    revision: str
    dimension: int
    max_tokens: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Unit-length vectors, one per text (so cosine similarity is a dot product)."""
        ...

    def count_tokens(self, text: str) -> int: ...


def _snapshot_dir(settings: Settings) -> Path:
    """Where the pinned revision lives inside the Hugging Face cache layout."""
    repo_dir = "models--" + settings.embedding_model.replace("/", "--")
    return Path(settings.models_dir) / repo_dir / "snapshots" / settings.embedding_revision


def model_is_downloaded(settings: Settings) -> bool:
    snapshot = _snapshot_dir(settings)
    return (snapshot / "model.safetensors").exists() and (snapshot / "config.json").exists()


def embedding_status(settings: Settings) -> EmbeddingStatus:
    """Cheap check (does not load the model) used by health checks and the UI."""
    base = {"model": settings.embedding_model, "revision": settings.embedding_revision}
    if not settings.embeddings_enabled:
        return EmbeddingStatus(
            available=False, enabled=False, reason="Embeddings are turned off (CLEARCASE_EMBEDDINGS_ENABLED=false).", **base
        )
    if importlib.util.find_spec("sentence_transformers") is None:
        return EmbeddingStatus(
            available=False, enabled=True, reason="The sentence-transformers package is not installed.", **base
        )
    if not model_is_downloaded(settings):
        return EmbeddingStatus(
            available=False,
            enabled=True,
            reason=(
                "The embedding model has not been downloaded. Run scripts/download_models.sh once "
                "(Docker: docker compose run --rm backend python -m app.cli download-embedding-model)."
            ),
            **base,
        )
    return EmbeddingStatus(available=True, enabled=True, dimension=384, **base)


class SentenceTransformerEmbedder:
    """The real embedder: a pretrained sentence-transformers model, loaded lazily and offline."""

    def __init__(self, model_id: str, revision: str, models_dir: str) -> None:
        self.model_id = model_id
        self.revision = revision
        self._models_dir = models_dir
        self._model = None
        self._lock = threading.Lock()  # loading and encoding are serialised: simple and safe
        self.dimension = 0
        self.max_tokens = 0

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                self.model_id,
                revision=self.revision,
                cache_folder=self._models_dir,
                device="cpu",
                trust_remote_code=False,
                local_files_only=True,  # never touch the network while serving
            )
        except Exception as exc:
            logger.warning("Embedding model could not be loaded: %s", type(exc).__name__)
            raise EmbeddingUnavailable(
                "The embedding model could not be loaded from the local cache. "
                "Run scripts/download_models.sh once, then restart."
            ) from exc
        self.dimension = int(model.get_embedding_dimension())
        self.max_tokens = int(model.max_seq_length)
        self._model = model
        return model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            model = self._load()
            vectors = model.encode(
                list(texts), normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False, batch_size=32
            )
        return [[float(x) for x in row] for row in vectors]

    def count_tokens(self, text: str) -> int:
        with self._lock:
            model = self._load()
            return len(model.tokenizer.encode(text, add_special_tokens=True))


@lru_cache(maxsize=4)
def _cached_embedder(model_id: str, revision: str, models_dir: str) -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(model_id, revision, models_dir)


def get_embedder(settings: Settings) -> Embedder:
    """The process-wide embedder, or EmbeddingUnavailable with the reason."""
    status = embedding_status(settings)
    if not status.available:
        raise EmbeddingUnavailable(status.reason or "Embeddings are unavailable.")
    return _cached_embedder(settings.embedding_model, settings.embedding_revision, settings.models_dir)


def download_embedding_model(settings: Settings) -> EmbeddingStatus:
    """One-time download of the pinned model into ``models_dir`` (this is the only networked step)."""
    from sentence_transformers import SentenceTransformer

    Path(settings.models_dir).mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(
        settings.embedding_model,
        revision=settings.embedding_revision,
        cache_folder=settings.models_dir,
        device="cpu",
        trust_remote_code=False,
    )
    # Prove it really works before declaring success.
    vector = model.encode(["download check"], normalize_embeddings=True)[0]
    if len(vector) != model.get_embedding_dimension():
        raise RuntimeError("Downloaded model returned an unexpected vector size.")
    return embedding_status(settings)
