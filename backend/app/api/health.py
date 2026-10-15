"""Health endpoint used by the frontend banner, Docker and CI."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_mongo
from app.config import Settings, get_settings
from app.db import Mongo
from app.services.detection import ner_info
from app.services.embeddings import EmbeddingStatus, embedding_status
from app.services.pdf_processing import ENGINE_NAME, ENGINE_VERSION

router = APIRouter(tags=["health"])


class AssistantInfo(BaseModel):
    mode: str  # "demo" (scripted, no model) or "live" (a hosted model)
    provider: str | None = None
    model: str | None = None
    configured: bool  # live mode has a provider, key and model; demo mode always counts as configured
    sends_excerpts_to_provider: bool  # true in live mode: document excerpts leave this machine


class HealthResponse(BaseModel):
    status: str
    mongodb: bool
    assistant_mode: str
    assistant: AssistantInfo
    pdf_engine: str
    ner_available: bool
    ner_model: str | None
    embeddings: EmbeddingStatus


def _assistant_info(settings: Settings) -> AssistantInfo:
    if settings.assistant_mode != "live":
        return AssistantInfo(mode="demo", configured=True, sends_excerpts_to_provider=False)
    provider = settings.llm_provider.strip().lower()
    return AssistantInfo(
        mode="live",
        provider=provider or None,
        model=settings.llm_model.strip() or None,
        configured=provider == "anthropic" and bool(settings.llm_api_key.strip()) and bool(settings.llm_model.strip()),
        sends_excerpts_to_provider=True,
    )


@router.get("/health", response_model=HealthResponse)
def health(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    db_ok = mongo.ping()
    ner = ner_info()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        mongodb=db_ok,
        assistant_mode=settings.assistant_mode,
        assistant=_assistant_info(settings),
        pdf_engine=f"{ENGINE_NAME} {ENGINE_VERSION}",
        ner_available=ner.available,
        ner_model=ner.id if ner.available else None,
        embeddings=embedding_status(settings),
    )
