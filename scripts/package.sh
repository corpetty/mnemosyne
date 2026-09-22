#!/bin/bash
# Build distributable bundles. Tauri runs scripts/build-all.sh first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"
# NO_STRIP: linuxdeploy's bundled strip (2024) cannot parse .relr.dyn sections in
# current glibc/zstd builds and aborts the AppImage; the binaries are fine unstripped.
NO_STRIP=true WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm tauri build "$@"

echo ""
echo "Artifacts:"
ls -lh src-tauri/target/release/bundle/appimage/*.AppImage 2>/dev/null || echo "  No AppImage"
ls -lh src-tauri/target/release/bundle/deb/*.deb 2>/dev/null || echo "  No deb"
ls -lh src-tauri/target/release/bundle/rpm/*.rpm 2>/dev/null || echo "  No rpm"
