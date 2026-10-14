"""A tiny deterministic embedder for tests of *logic* (versioning, ownership, staleness...).

It hashes words into a 64-number vector, so texts that share words are similar. It
is NOT a model of meaning and is never used to judge retrieval quality; those tests
use the real local model (``tests/test_retrieval_semantic.py``).
"""

from __future__ import annotations

import math
import re
import zlib
from collections.abc import Sequence

DIM = 64
_STOP = {"the", "and", "for", "with", "that", "this", "are", "you", "our", "any", "not", "but"}


class FakeEmbedder:
    def __init__(self, *, model_id: str = "fake/bow", revision: str = "fake-rev-1", max_tokens: int = 256) -> None:
        self.model_id = model_id
        self.revision = revision
        self.dimension = DIM
        self.max_tokens = max_tokens
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for word in re.findall(r"[a-z0-9]{3,}", text.lower()):
                if word not in _STOP:
                    vec[zlib.crc32(word.encode()) % DIM] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec] if any(vec) else [1.0] + [0.0] * (DIM - 1))
        return out

    def count_tokens(self, text: str) -> int:
        return len(text.split())
