#!/usr/bin/env bash
# Backend for the browser end-to-end tests (frontend/e2e).
#
# - Uses the scripted DEMO assistant and blanks the provider settings, so these tests can never call a paid model,
#   even if your .env holds a key (environment variables beat .env).
# - Uses its own database, `clearcase_e2e`, which is DROPPED every time this script starts. Your real data is untouched.
# - Listens on 127.0.0.1:8011 (change with E2E_API_PORT) so it does not collide with `scripts/dev_backend.sh`.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/backend"

export CLEARCASE_MONGODB_DB=clearcase_e2e
export CLEARCASE_ASSISTANT_MODE=demo
export CLEARCASE_LLM_PROVIDER=
export CLEARCASE_LLM_API_KEY=
export CLEARCASE_LLM_MODEL=

# Locally the backend lives in backend/.venv; in CI the dependencies are installed into the job's own Python.
if [[ -x .venv/bin/python ]]; then PY=.venv/bin/python; UVICORN=.venv/bin/uvicorn; else PY=python; UVICORN=uvicorn; fi

"$PY" - <<'PY'
import os
from pymongo import MongoClient

client = MongoClient(os.environ.get("CLEARCASE_MONGODB_URI", "mongodb://127.0.0.1:27017"), serverSelectionTimeoutMS=5000)
client.drop_database(os.environ["CLEARCASE_MONGODB_DB"])
PY

exec "$UVICORN" app.main:app --host 127.0.0.1 --port "${E2E_API_PORT:-8011}"
