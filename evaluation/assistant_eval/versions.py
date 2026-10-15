"""Which models and policies an evaluation used. The specification asks for model and policy versions to be recorded.

The seeded policies are plain files, so their id, version and a hash of their bytes are read from the files themselves; the embedding
model and its pinned revision, and the retrieval settings, come from the application's own configuration. The same helper feeds the
runner (new run records carry it) and the report (existing records predate it, so the report reads the same files; git shows the
policy files unchanged since 2026-10-13, before every measured run).
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.services.policy_chunking import parse_front_matter, slugify  # noqa: E402

POLICY_DIR = ROOT / "sample_data" / "policies"


def policy_versions(directory: Path = POLICY_DIR) -> list[dict]:
    out = []
    for path in sorted(directory.glob("*.md")):
        data = path.read_bytes()
        meta, _ = parse_front_matter(data.decode("utf-8-sig"))
        out.append({"policy_id": meta.get("policy_id") or slugify(path.stem), "version": meta.get("version"), "file": path.name, "sha256": hashlib.sha256(data).hexdigest()})
    return out


def embedding_info() -> dict:
    s = Settings(_env_file=None)
    return {"model": s.embedding_model, "revision": s.embedding_revision, "similarity_cutoff": s.retrieval_min_score, "keyword_weight": s.retrieval_keyword_weight}


def provenance(assistant_models: Sequence[str]) -> dict:
    return {"assistant_models": sorted(assistant_models), "policies": policy_versions(), "embedding": embedding_info()}


def provenance_line(p: dict) -> str:
    policies = "; ".join(f"{x['policy_id']} v{x['version']} (sha256 {x['sha256'][:12]})" for x in p["policies"])
    e = p["embedding"]
    models = ", ".join(p["assistant_models"]) or "none run yet"
    return (
        f"Assistant models: {models}. Policies: {policies}. Embedding model: {e['model']} at revision {e['revision'][:12]}, "
        f"similarity cutoff {e['similarity_cutoff']}, keyword weight {e['keyword_weight']}."
    )
