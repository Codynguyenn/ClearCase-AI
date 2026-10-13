#!/usr/bin/env bash
# Run a real MongoDB Community Server locally WITHOUT Docker or Homebrew.
#
# Why this exists: the project needs a genuine MongoDB (GridFS, indexes, the
# official driver). On a machine without Docker, the simplest honest option is
# to download the official binary tarball once and run `mongod` bound to
# localhost. Everything lives under the git-ignored `.local/` folder.
#
# Usage:
#   scripts/dev_mongo.sh start    # download (first time only) and start
#   scripts/dev_mongo.sh stop
#   scripts/dev_mongo.sh status
#   scripts/dev_mongo.sh reset    # stop, wipe data directory, start fresh
#
# Apple Silicon (arm64) macOS is the supported target for this script.
set -euo pipefail

MONGO_VERSION="${MONGO_VERSION:-8.0.34}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="$ROOT_DIR/.local"
MONGO_DIR="$LOCAL_DIR/mongodb"
DATA_DIR="$LOCAL_DIR/mongodb-data"
LOG_FILE="$LOCAL_DIR/mongod.log"
PID_FILE="$LOCAL_DIR/mongod.pid"
PORT="${MONGO_PORT:-27017}"

download() {
  if [[ -x "$MONGO_DIR/bin/mongod" ]]; then
    return
  fi
  local arch os url tgz
  arch="$(uname -m)"
  os="$(uname -s)"
  if [[ "$os" != "Darwin" || "$arch" != "arm64" ]]; then
    echo "This helper only auto-downloads for macOS arm64. Use docker compose or install MongoDB manually." >&2
    exit 1
  fi
  url="https://fastdl.mongodb.org/osx/mongodb-macos-arm64-${MONGO_VERSION}.tgz"
  tgz="$LOCAL_DIR/mongodb.tgz"
  mkdir -p "$LOCAL_DIR"
  echo "Downloading MongoDB Community ${MONGO_VERSION} from $url ..."
  curl -fL --progress-bar -o "$tgz" "$url"
  tar xzf "$tgz" -C "$LOCAL_DIR"
  # The extracted folder name varies slightly between releases; normalise it.
  local extracted
  extracted="$(find "$LOCAL_DIR" -maxdepth 1 -type d -name 'mongodb-macos-*' | head -n 1)"
  mv "$extracted" "$MONGO_DIR"
  rm -f "$tgz"
  echo "Installed to $MONGO_DIR"
}

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start() {
  download
  if is_running; then
    echo "mongod already running (pid $(cat "$PID_FILE")) on 127.0.0.1:$PORT"
    return
  fi
  mkdir -p "$DATA_DIR"
  "$MONGO_DIR/bin/mongod" \
    --dbpath "$DATA_DIR" \
    --bind_ip 127.0.0.1 \
    --port "$PORT" \
    --fork \
    --logpath "$LOG_FILE" \
    --pidfilepath "$PID_FILE" >/dev/null
  echo "mongod started on mongodb://127.0.0.1:$PORT (log: $LOG_FILE)"
}

stop() {
  if is_running; then
    kill "$(cat "$PID_FILE")"
    echo "mongod stopped"
  else
    echo "mongod is not running"
  fi
  rm -f "$PID_FILE"
}

status() {
  if is_running; then
    echo "running (pid $(cat "$PID_FILE")) on 127.0.0.1:$PORT"
  else
    echo "stopped"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  reset)
    stop
    rm -rf "$DATA_DIR"
    start
    ;;
  *)
    echo "Usage: $0 {start|stop|status|reset}" >&2
    exit 1
    ;;
esac
