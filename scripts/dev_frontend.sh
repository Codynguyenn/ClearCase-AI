#!/usr/bin/env bash
# Start the Vite dev server on http://127.0.0.1:5173 (proxies /api to the backend).
# Uses the system Node if it is >= 22.12, otherwise the one from scripts/dev_node.sh.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
eval "$("$ROOT_DIR/scripts/dev_node.sh")"
cd "$ROOT_DIR/frontend"

if [[ ! -d node_modules ]]; then
  echo "Installing frontend dependencies..." >&2
  npm ci
fi

exec npm run dev
