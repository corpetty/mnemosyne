#!/bin/bash
# Build the remote-access sidecar (link/, iroh) as a Tauri sidecar binary.
# Output: src-tauri/binaries/mnemosyne-link-<target-triple>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_ROOT/src-tauri/binaries"
TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"
OUT="$OUT_DIR/mnemosyne-link-$TRIPLE"

cargo build --release --locked --manifest-path "$PROJECT_ROOT/link/Cargo.toml"
mkdir -p "$OUT_DIR"
cp "$PROJECT_ROOT/link/target/release/mnemosyne-link" "$OUT"
chmod +x "$OUT"
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
