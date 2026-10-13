"""FastAPI application factory.

Run locally with:
    cd backend && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo.errors import PyMongoError

from app.api import documents, exports, health, review
from app.api.errors import register_error_handlers
from app.config import Settings, get_settings
from app.db import Mongo
from app.services.document_service import DocumentService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("clearcase")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mongo = Mongo(settings)
        app.state.mongo = mongo
        try:
            mongo.ensure_indexes()
            interrupted = DocumentService(mongo, settings).recover_interrupted()
            logger.info(
                "Connected to MongoDB %s/%s (recovered %d interrupted job(s))",
                settings.mongodb_uri,
                settings.mongodb_db,
                interrupted,
            )
        except PyMongoError as exc:
            # Keep serving so /api/health can report the problem instead of crash-looping.
            logger.error("MongoDB unavailable at startup: %s", exc)
        yield
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
    return app


app = create_app()
