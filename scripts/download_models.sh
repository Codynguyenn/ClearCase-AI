#!/usr/bin/env bash
# One-time downloads of the pretrained models ClearCase uses. Run once; after that
# everything runs offline (the app only ever loads models from the local cache).
#
#   1. spaCy `en_core_web_sm` (~12 MB): people / organisations / locations (NER).
#      Detection works without it (patterns only) and says so.
#   2. sentence-transformers `all-MiniLM-L6-v2`, pinned to an exact revision (~90 MB):
#      embeddings for policy retrieval. Without it, semantic policy search reports
#      "unavailable"; it never falls back to keyword search and calls that semantic.
#      Stored in .local/models (git-ignored) or $CLEARCASE_MODELS_DIR.
#
# We did not train either model; both are pretrained and used as published.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT_DIR/backend/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "Create the backend virtualenv first: scripts/dev_backend.sh" >&2
  exit 1
fi

if "$PY" -c "import spacy; spacy.load('en_core_web_sm')" >/dev/null 2>&1; then
  echo "spaCy en_core_web_sm is already installed."
else
  echo "Downloading spaCy model en_core_web_sm (~12 MB)..."
  "$PY" -m spacy download en_core_web_sm
fi

cd "$ROOT_DIR/backend"
"$PY" -m app.cli download-embedding-model
echo "Done. Restart the backend if it is already running."
