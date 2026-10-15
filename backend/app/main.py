"""FastAPI application factory.

Run locally with:
    cd backend && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError

from app.agent.runs import RunManager
from app.api import conversations, documents, exports, health, policies, review
from app.api.errors import register_error_handlers
from app.api.upload_limit import UploadRule, UploadSizeLimitMiddleware
from app.config import Settings, get_settings
from app.db import Mongo
from app.services.conversation_service import recover_interrupted_runs
from app.services.document_service import DocumentService
from app.services.embeddings import embedding_status
from app.services.policy_service import EmbedderProvider, PolicyService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("clearcase")


def start_background_reindex(
    mongo: Mongo, settings: Settings, embedder_provider: EmbedderProvider | None = None
) -> threading.Thread | None:
    """Refresh stale or missing policy embeddings without delaying startup. Never raises into the app.

    Skipped when no embedding model can be used (nothing would succeed, and each attempt would touch the policies).
    """
    if embedder_provider is None and not embedding_status(settings).available:
        logger.info("Startup re-index skipped: the embedding model is not available.")
        return None

    def work() -> None:
        try:
            service = PolicyService(mongo, settings, embedder_provider)
            refreshed = sum(service.reindex_all(owner) for owner in service.policies.owners())
            if refreshed:
                logger.info("Startup re-index refreshed %d policy version(s).", refreshed)
        except Exception:  # noqa: BLE001 - a failed refresh must never stop the app; the Re-index button still works
            logger.exception("Startup re-index failed; use the Re-index button in the Policies tab.")

    thread = threading.Thread(target=work, name="policy-reindex", daemon=True)
    thread.start()
    return thread


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mongo = Mongo(settings)
        app.state.mongo = mongo
        # Tests may set `assistant_factory` / `tool_factory` on app.state before startup to use a scripted model.
        manager = RunManager(
            mongo,
            settings,
            assistant_factory=getattr(app.state, "assistant_factory", None),
            tool_factory=getattr(app.state, "tool_factory", None),
        )
        app.state.run_manager = manager
        await manager.start_background()
        try:
            mongo.ensure_indexes()
            interrupted = DocumentService(mongo, settings).recover_interrupted()
            interrupted_runs = recover_interrupted_runs(mongo)
            logger.info(
                "Connected to MongoDB %s/%s (recovered %d interrupted job(s), %d assistant run(s))",
                settings.mongodb_uri,
                settings.mongodb_db,
                interrupted,
                interrupted_runs,
            )
            if settings.reindex_on_startup:
                app.state.reindex_thread = start_background_reindex(
                    mongo, settings, getattr(app.state, "embedder_provider", None)
                )
        except PyMongoError as exc:
            # Keep serving so /api/health can report the problem instead of crash-looping.
            logger.error("MongoDB unavailable at startup: %s", exc)
        yield
        await manager.shutdown()  # stop runs and tool servers while the database is still open
        thread = getattr(app.state, "reindex_thread", None)
        if thread is not None:
            thread.join(timeout=10)  # let a refresh in progress finish its current write before the DB closes
        mongo.close()

    app = FastAPI(
        title="ClearCase API",
        version="0.1.0",
        description="AI-assisted legal document redaction (local MVP).",
        lifespan=lifespan,
    )
    # Make every `Depends(get_settings)` resolve to *this* settings object so tests
    # (and any future multi-config setup) cannot drift from what the app was built with.
    app.dependency_overrides[get_settings] = lambda: settings
    # Added before CORS so CORS stays outermost: a 413 must still carry CORS headers for the browser to read it.
    app.add_middleware(
        UploadSizeLimitMiddleware,
        rules=[
            UploadRule("/api/documents", settings.max_upload_bytes, "file_too_large", "PDF"),
            UploadRule("/api/policies", settings.max_policy_kb * 1024, "policy_too_large", "policy file"),
        ],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.include_router(health.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    app.include_router(review.router, prefix="/api")
    app.include_router(exports.router, prefix="/api")
    app.include_router(policies.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    return app


app = create_app()
