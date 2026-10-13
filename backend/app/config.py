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
