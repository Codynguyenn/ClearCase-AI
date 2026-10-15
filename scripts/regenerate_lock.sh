#!/usr/bin/env bash
# Rebuild backend/requirements.lock from backend/pyproject.toml.
#
# Why a script: the lock is a `pip freeze` of a clean virtualenv, but a plain freeze breaks Linux installs. On
# macOS, PyPI's torch wheel is small; on Linux, `pip install torch` pulls several GB of NVIDIA CUDA packages we
# never use, so Docker and CI need PyTorch's CPU-only build from its own index. The lock therefore needs two
# `torch` lines with platform markers plus an `--extra-index-url` header, which `pip freeze` cannot produce.
# This script runs the freeze and then adds exactly those pieces back.
#
# Usage (from anywhere):
#   scripts/regenerate_lock.sh              # uses python3
#   scripts/regenerate_lock.sh python3.12   # use a specific interpreter
#
# This re-resolves everything: pinned direct dependencies stay as written in pyproject.toml, but transitive
# dependencies move to their newest compatible releases. Run the backend tests afterwards and read the diff.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYPROJECT="$ROOT_DIR/backend/pyproject.toml"
CURRENT_LOCK="$ROOT_DIR/backend/requirements.lock"
# LOCK_OUT lets you write somewhere else, e.g. to compare against the committed lock first.
LOCK="${LOCK_OUT:-$CURRENT_LOCK}"
PY="${1:-python3}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Creating a clean virtualenv with $("$PY" --version) ..."
"$PY" -m venv "$WORK/venv"

# Direct dependencies plus the dev extras (the lock is what CI installs before running pytest), plus any
# "name @ https://..." pins already in the lock. Those are not on PyPI and cannot be re-resolved, so they are
# carried over unchanged; today that is the spaCy English model wheel, pinned by URL and sha256.
"$PY" - "$PYPROJECT" "$CURRENT_LOCK" >"$WORK/direct.txt" <<'PYEOF'
import os
import sys
import tomllib

pyproject_path, lock_path = sys.argv[1:3]
with open(pyproject_path, "rb") as handle:
    project = tomllib.load(handle)["project"]
requirements = project["dependencies"] + project.get("optional-dependencies", {}).get("dev", [])
if os.path.exists(lock_path):
    with open(lock_path, encoding="utf-8") as handle:
        requirements += [line.strip() for line in handle if " @ " in line and not line.lstrip().startswith("#")]
print("\n".join(requirements))
PYEOF

echo "Installing $(wc -l <"$WORK/direct.txt" | tr -d ' ') direct dependencies (a few minutes; PyTorch is large) ..."
"$WORK/venv/bin/python" -m pip install --quiet -r "$WORK/direct.txt"
"$WORK/venv/bin/python" -m pip freeze >"$WORK/freeze.txt"

"$PY" - "$WORK/freeze.txt" "$LOCK" <<'PYEOF'
import re
import sys

freeze_path, lock_path = sys.argv[1:3]
HEADER = """\
# Pinned transitive dependencies (see README "Dependencies").
# torch: macOS uses the PyPI wheel; Linux uses the CPU-only build from PyTorch's own index.
# Without this, `pip install torch` on Linux pulls several GB of NVIDIA CUDA packages we never use.
# Regenerate with scripts/regenerate_lock.sh (a plain `pip freeze` would lose the torch markers).
--extra-index-url https://download.pytorch.org/whl/cpu
"""
lines = []
torch_version = None
for raw in open(freeze_path, encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    match = re.match(r"^torch==([^+\s]+)", line)
    if match:
        torch_version = match.group(1)
        continue
    lines.append(line)
if torch_version is None:
    sys.exit("torch was not installed, so the markers cannot be written. Is it still in pyproject.toml?")
lines.append(f'torch=={torch_version}; sys_platform == "darwin"')
lines.append(f'torch=={torch_version}+cpu; sys_platform == "linux"')
lines.sort(key=lambda line: re.split(r"[=@\s;\[]", line, maxsplit=1)[0].lower())  # by name, like pip freeze
with open(lock_path, "w", encoding="utf-8") as out:
    out.write(HEADER + "\n".join(lines) + "\n")
print(f"Wrote {lock_path} ({len(lines)} pins, torch {torch_version} with platform markers)")
PYEOF

echo "Done. Review with: git diff backend/requirements.lock   then run the backend tests."
