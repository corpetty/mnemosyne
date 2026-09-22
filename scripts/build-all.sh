#!/bin/bash
# Called by Tauri's beforeBuildCommand. Produces everything the bundle needs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "--- Frontend ---"
(cd "$PROJECT_ROOT" && pnpm build)

echo "--- Backend sources ---"
bash "$SCRIPT_DIR/stage-backend.sh"

echo "--- uv sidecar ---"
bash "$SCRIPT_DIR/fetch-uv.sh"
