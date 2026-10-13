#!/usr/bin/env bash
# Provide a Node.js that meets the frontend toolchain requirement (>= 22.12)
# WITHOUT changing the system installation.
#
# Vite 8, its bundler (rolldown) and pdfjs-dist 6 all declare `engines.node`
# ">= 22.12". With an older Node, npm silently skips *optional* native
# dependencies that fail the engine check, and the build fails with
# "Cannot find native binding". Upgrading Node is the real fix; this script is
# the zero-admin alternative: it downloads the official LTS tarball into the
# git-ignored `.local/node` folder.
#
# Usage:
#   scripts/dev_node.sh            # download if needed, print the PATH line
#   eval "$(scripts/dev_node.sh)"  # put it on PATH in the current shell
set -euo pipefail

NODE_VERSION="${NODE_VERSION:-22.23.3}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_DIR="$ROOT_DIR/.local/node"

required_major=22
required_minor=12

version_ok() {
  local v major minor
  v="$(node --version 2>/dev/null | sed 's/^v//')" || return 1
  major="${v%%.*}"
  minor="$(echo "$v" | cut -d. -f2)"
  (( major > required_major || (major == required_major && minor >= required_minor) ))
}

if version_ok; then
  # System Node is fine; nothing to do. Emit a no-op so `eval` is harmless.
  echo "# system node $(node --version) satisfies >= ${required_major}.${required_minor}" >&2
  echo "true"
  exit 0
fi

if [[ ! -x "$NODE_DIR/bin/node" ]]; then
  if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "Auto-download only supports macOS arm64. Install Node >= 22.12 from https://nodejs.org" >&2
    exit 1
  fi
  url="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-darwin-arm64.tar.gz"
  echo "Downloading Node ${NODE_VERSION} from $url ..." >&2
  mkdir -p "$ROOT_DIR/.local"
  curl -fL --progress-bar -o "$ROOT_DIR/.local/node.tar.gz" "$url"
  tar xzf "$ROOT_DIR/.local/node.tar.gz" -C "$ROOT_DIR/.local"
  mv "$ROOT_DIR/.local/node-v${NODE_VERSION}-darwin-arm64" "$NODE_DIR"
  rm -f "$ROOT_DIR/.local/node.tar.gz"
  echo "Installed to $NODE_DIR" >&2
fi

echo "export PATH=\"$NODE_DIR/bin:\$PATH\""
