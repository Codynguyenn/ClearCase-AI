#!/usr/bin/env bash
# One-time download of the pretrained spaCy English NER model.
#
# Detection works without this (pattern-only). With it, people, organisations
# and locations are also proposed as review-needed candidates. We did not train
# this model; it is the official spaCy `en_core_web_sm` pipeline.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT_DIR/backend/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "Create the backend virtualenv first: scripts/dev_backend.sh" >&2
  exit 1
fi

if "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
  echo "en_core_web_sm is already installed."
  exit 0
fi

echo "Downloading spaCy model en_core_web_sm (~12 MB)..."
"$PY" -m spacy download en_core_web_sm
echo "Done. Restart the backend if it is already running."
