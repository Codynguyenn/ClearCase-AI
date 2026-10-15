"""FastAPI dependencies.

``get_owner_id`` is the single place that decides "who is making this request".
Today it returns the server-configured demo identity. When real authentication
arrives, only this function changes; every route and repository already expects
an owner id.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.agent.runs import RunManager
from app.config import Settings, get_settings
from app.db import Mongo
from app.services.conversation_service import ConversationService
from app.services.document_service import DocumentService
from app.services.export_service import ExportService
from app.services.policy_service import PolicyService
from app.services.retrieval import MongoCosineRetriever, PolicyRetriever
from app.services.review_service import ReviewService


def get_mongo(request: Request) -> Mongo:
    return request.app.state.mongo


def get_owner_id(settings: Annotated[Settings, Depends(get_settings)]) -> str:
    return settings.demo_owner_id


def get_document_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentService:
    return DocumentService(mongo, settings)


def get_review_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewService:
    return ReviewService(mongo, settings)


def get_export_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExportService:
    return ExportService(mongo, settings)


def get_policy_service(
    request: Request,
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PolicyService:
    # `embedder_provider` is set on app.state so tests can substitute a fake embedder.
    return PolicyService(mongo, settings, getattr(request.app.state, "embedder_provider", None))


def get_retriever(
    request: Request,
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PolicyRetriever:
    return MongoCosineRetriever(mongo, settings, getattr(request.app.state, "embedder_provider", None))


def get_conversation_service(
    mongo: Annotated[Mongo, Depends(get_mongo)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationService:
    return ConversationService(mongo, settings)


def get_run_manager(request: Request) -> RunManager:
    return request.app.state.run_manager


OwnerId = Annotated[str, Depends(get_owner_id)]
DocService = Annotated[DocumentService, Depends(get_document_service)]
ReviewSvc = Annotated[ReviewService, Depends(get_review_service)]
ExportSvc = Annotated[ExportService, Depends(get_export_service)]
PolicySvc = Annotated[PolicyService, Depends(get_policy_service)]
Retriever = Annotated[PolicyRetriever, Depends(get_retriever)]
ConversationSvc = Annotated[ConversationService, Depends(get_conversation_service)]
Runs = Annotated[RunManager, Depends(get_run_manager)]
