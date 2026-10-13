#!/usr/bin/env bash
# Start the FastAPI backend with auto-reload on http://127.0.0.1:8000
# Creates the virtualenv and installs pinned dependencies on first run.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/backend"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating virtualenv..." >&2
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.lock
fi

exec .venv/bin/uvicorn app.main:app --reload --host "${CLEARCASE_HOST:-127.0.0.1}" --port "${CLEARCASE_PORT:-8000}"
