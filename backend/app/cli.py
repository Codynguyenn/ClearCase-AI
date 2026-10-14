"""Small command-line helpers: ``python -m app.cli <command>`` (run from ``backend/``).

Commands
--------
download-embedding-model   one-time download of the pinned embedding model
embedding-status           print whether semantic retrieval can run, and why not
"""

from __future__ import annotations

import argparse
import sys

from app.config import Settings
from app.services.embeddings import download_embedding_model, embedding_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=["download-embedding-model", "embedding-status"])
    args = parser.parse_args(argv)
    settings = Settings()

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
