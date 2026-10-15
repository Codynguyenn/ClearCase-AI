"""Small command-line helpers: ``python -m app.cli <command>`` (run from ``backend/``).

Commands
--------
download-embedding-model   one-time download of the pinned embedding model
embedding-status           print whether semantic retrieval can run, and why not
seed-samples [--reset]     load the sample PDF(s) and fictional policies; --reset rebuilds only those samples
"""

from __future__ import annotations

import argparse
import sys

from pymongo.errors import PyMongoError

from app.config import Settings
from app.db import Mongo
from app.services.embeddings import download_embedding_model, embedding_status
from app.services.errors import AppError
from app.services.policy_service import EmbedderProvider
from app.services.sample_data import seed_sample_data


def seed_samples(settings: Settings, *, reset: bool, embedder_provider: EmbedderProvider | None = None) -> int:
    """Seed (or reset) the sample data for the configured demo user. Returns a process exit code."""
    mongo = Mongo(settings)
    try:
        if not mongo.ping():
            print(f"MongoDB is not reachable at {settings.mongodb_uri}. Start it first (scripts/dev_mongo.sh start).", file=sys.stderr)
            return 1
        mongo.ensure_indexes()
        owner = settings.demo_owner_id
        print(f"{'Resetting' if reset else 'Seeding'} sample data in database '{settings.mongodb_db}' for '{owner}'...")
        report = seed_sample_data(mongo, settings, owner, reset=reset, embedder_provider=embedder_provider)
    except (AppError, PyMongoError) as exc:
        print(f"Could not seed the samples: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 1
    finally:
        mongo.close()

    if report.documents_removed:
        print(f"  removed {report.documents_removed} old sample document(s) and everything derived from them")
    for doc in report.documents:
        print(f"  document  {doc.filename}  [{doc.status.value}]  {doc.page_count} pages")
    if reset and report.documents_created == 0:
        print("  (nothing was re-created)")
    for policy in report.policies:
        print(f"  policy    {policy.policy_id} v{policy.version}  [{policy.status.value}, embeddings {policy.embedding.state.value}]")
    if reset:
        print("Note: --reset also removes any other versions of the sample policies that you uploaded yourself.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["download-embedding-model", "embedding-status", "seed-samples"])
    parser.add_argument("--reset", action="store_true", help="with seed-samples: delete the existing samples first")
    args = parser.parse_args(argv)
    if args.reset and args.command != "seed-samples":
        parser.error("--reset only applies to seed-samples")
    settings = Settings()

    if args.command == "seed-samples":
        return seed_samples(settings, reset=args.reset)

    if args.command == "embedding-status":
        status = embedding_status(settings)
        print(status.model_dump_json(indent=2))
        return 0 if status.available else 1

    if embedding_status(settings).available:
        print(f"Embedding model already downloaded: {settings.embedding_model}@{settings.embedding_revision[:12]}")
        return 0
    print(f"Downloading {settings.embedding_model} (revision {settings.embedding_revision[:12]}, ~90 MB)...")
    status = download_embedding_model(settings)
    print(status.model_dump_json(indent=2))
    return 0 if status.available else 1


if __name__ == "__main__":
    sys.exit(main())
