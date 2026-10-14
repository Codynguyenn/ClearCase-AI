"""Application settings.

All configuration comes from environment variables prefixed with ``CLEARCASE_``
(or a local ``.env`` file). Pydantic validates types at startup so a typo such as
``CLEARCASE_MAX_UPLOAD_MB=twenty`` fails immediately with a clear message instead
of surfacing as a confusing runtime error later.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The repository root holds the shared `.env` file (one level above `backend/`).
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLEARCASE_",
        env_file=(_REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    mongodb_uri: str = "mongodb://127.0.0.1:27017"
    mongodb_db: str = "clearcase"

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Demo identity (not authentication; see docs/decisions.md) ---
    demo_owner_id: str = "demo-user"

    # --- Upload limits ---
    max_upload_mb: int = Field(default=20, ge=1, le=500)
    max_pages: int = Field(default=200, ge=1, le=5000)

    # --- Retrieval / embeddings (Phase 4) ---
    # Models are downloaded once into `models_dir` (scripts/download_models.sh) and are only ever
    # loaded from there at runtime: the app never downloads anything while serving a request.
    models_dir: str = str(_REPO_ROOT / ".local" / "models")
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Exact commit of the model repository (its "version"). Recorded on every stored embedding.
    embedding_revision: str = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    embeddings_enabled: bool = True
    max_policy_kb: int = Field(default=200, ge=1, le=2048)
    # Where the fictional example policies live (in Docker this folder is mounted read-only).
    sample_data_dir: str = str(_REPO_ROOT / "sample_data")
    retrieval_top_k_max: int = Field(default=5, ge=1, le=20)
    # Similarity cutoff: a passage is shown only if its plain cosine similarity to the question reaches
    # this value. It is a HEURISTIC chosen by evaluation/tune_retrieval.py on a small synthetic dev set
    # (middle of the best plateau, balanced accuracy 0.84; see evaluation/results/retrieval_dev.json).
    # It is specific to the default embedding model revision; re-run the script if you change the model.
    retrieval_min_score: float = Field(default=0.38, ge=0.0, le=1.0)
    # Weight of keyword overlap when ORDERING passages that already passed the cutoff (not used for the cutoff).
    retrieval_keyword_weight: float = Field(default=0.15, ge=0.0, le=1.0)

    # --- Assistant (Phase 5) ---
    assistant_mode: str = "demo"
    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after first load)."""
    return Settings()
