#!/bin/bash
# Called by Tauri's beforeBuildCommand. Produces everything the bundle needs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "--- Frontend ---"
(cd "$PROJECT_ROOT" && pnpm build)
# The same web app, served by the backend to a team on the network (services/team_host.py).
rm -rf "$PROJECT_ROOT/src-tauri/resources/web"
mkdir -p "$PROJECT_ROOT/src-tauri/resources"
cp -r "$PROJECT_ROOT/build" "$PROJECT_ROOT/src-tauri/resources/web"

echo "--- Backend sources ---"
bash "$SCRIPT_DIR/stage-backend.sh"

echo "--- uv sidecar ---"
bash "$SCRIPT_DIR/fetch-uv.sh"

echo "--- remote-access sidecar ---"
bash "$SCRIPT_DIR/build-link.sh"
