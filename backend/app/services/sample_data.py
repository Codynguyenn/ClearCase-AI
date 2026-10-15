"""Load, and reset, the synthetic sample data: the fictional policies and the sample PDFs.

A sample document is recognised by the SHA-256 of its bytes, never by its name, so a reset can only ever remove
documents that are byte-for-byte the shipped samples. Anything else the user uploaded is left alone, and so are
the review findings and exports of other documents.

Run it with ``python -m app.cli seed-samples`` (add ``--reset`` to rebuild from scratch).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.db import Mongo
from app.models.documents import DocumentRecord
from app.models.policies import PolicyRecord
from app.services.document_service import DocumentService
from app.services.errors import NotFoundError
from app.services.policy_service import EmbedderProvider, PolicyService

logger = logging.getLogger(__name__)


@dataclass
class SeedReport:
    documents: list[DocumentRecord] = field(default_factory=list)  # present after seeding (new or already there)
    documents_created: int = 0
    documents_removed: int = 0  # old sample copies deleted by --reset (with their findings, exports and audit events)
    policies: list[PolicyRecord] = field(default_factory=list)


def sample_pdf_paths(sample_dir: Path) -> list[Path]:
    paths = sorted(sample_dir.glob("*.pdf"))
    if not paths:
        raise NotFoundError(
            f"No sample PDFs found in {sample_dir}. Generate them with: backend/.venv/bin/python sample_data/generate_samples.py",
            code="no_sample_documents",
        )
    return paths


def seed_sample_data(
    mongo: Mongo,
    settings: Settings,
    owner_id: str,
    *,
    reset: bool = False,
    embedder_provider: EmbedderProvider | None = None,
) -> SeedReport:
    sample_dir = Path(settings.sample_data_dir)
    pdfs = sample_pdf_paths(sample_dir)

    documents = DocumentService(mongo, settings)
    report = SeedReport()

    for path in pdfs:
        data = path.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        existing = documents.documents.find_by_hash(owner_id=owner_id, content_hash=content_hash)
        if reset:
            for old in existing:
                documents.delete(old.id, owner_id=owner_id)
                report.documents_removed += 1
            existing = []
        if existing:
            report.documents.append(existing[0])  # already loaded: seeding twice must not create duplicates
            continue
        record = documents.upload(owner_id=owner_id, filename=path.name, data=data)
        report.documents.append(documents.process(record.id, owner_id=owner_id))
        report.documents_created += 1

    policies = PolicyService(mongo, settings, embedder_provider)
    report.policies = policies.seed_samples(owner_id, sample_dir / "policies", reset=reset)
    logger.info(
        "Seeded samples for %s: %d new document(s), %d removed, %d policy(ies)",
        owner_id,
        report.documents_created,
        report.documents_removed,
        len(report.policies),
    )
    return report
