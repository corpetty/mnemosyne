#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "=== Packaging Mnemosyne ==="

# Build backend sidecar first (frontend is built by Tauri's beforeBuildCommand)
echo ""
echo "--- Building backend sidecar ---"
bash "$SCRIPT_DIR/build-backend.sh"

# Build Tauri app (triggers beforeBuildCommand -> build-all.sh -> frontend build)
echo ""
echo "--- Building Tauri app ---"
cd "$PROJECT_ROOT"
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm tauri build

echo ""
echo "=== Packaging complete ==="
echo ""
echo "Artifacts:"
ls -lh "$PROJECT_ROOT/src-tauri/target/release/bundle/appimage/"*.AppImage 2>/dev/null || echo "  No AppImage found"
ls -lh "$PROJECT_ROOT/src-tauri/target/release/bundle/deb/"*.deb 2>/dev/null || echo "  No deb found"
