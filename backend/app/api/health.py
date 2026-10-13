"""Health endpoint used by the frontend banner, Docker and CI."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_mongo
from app.config import Settings, get_settings
from app.db import Mongo
from app.services.detection import ner_info
from app.services.pdf_processing import ENGINE_NAME, ENGINE_VERSION

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    mongodb: bool
    assistant_mode: str
    pdf_engine: str
    ner_available: bool
    ner_model: str | None


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
        pdf_engine=f"{ENGINE_NAME} {ENGINE_VERSION}",
        ner_available=ner.available,
        ner_model=ner.id if ner.available else None,
    )
