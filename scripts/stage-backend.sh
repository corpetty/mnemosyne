#!/bin/bash
# Copy the backend source tree into src-tauri/resources/backend for bundling.
# No build step: the app installs dependencies with uv on first launch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SRC="$PROJECT_ROOT/backend"
DEST="$PROJECT_ROOT/src-tauri/resources/backend"

rm -rf "$DEST"
mkdir -p "$DEST"
cp "$SRC/pyproject.toml" "$SRC/uv.lock" "$SRC/main.py" "$SRC/.python-version" "$DEST/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$SRC/mnemosyne/" "$DEST/mnemosyne/"

echo "Staged backend to $DEST:"
du -sh "$DEST"
