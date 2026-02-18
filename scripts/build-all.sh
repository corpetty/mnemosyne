#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== Mnemosyne: Building All ==="

# Step 1: Build frontend (SvelteKit -> static files)
echo ""
echo "--- Step 1: Building frontend ---"
cd "$PROJECT_ROOT"
pnpm build

# Step 2: Build backend sidecar (PyInstaller)
echo ""
echo "--- Step 2: Building backend sidecar ---"
bash "$SCRIPT_DIR/build-backend.sh"

echo ""
echo "=== All builds complete ==="
