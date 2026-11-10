#!/bin/bash
# Build the audio capture sidecar (capture/) as a Tauri sidecar binary. macOS only in the app
# (it records with Core Audio there; Linux records with pw-record), but it builds anywhere.
# Output: src-tauri/binaries/mnemosyne-capture-<target-triple>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_ROOT/src-tauri/binaries"
TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"
OUT="$OUT_DIR/mnemosyne-capture-$TRIPLE"

cargo build --release --locked --manifest-path "$PROJECT_ROOT/capture/Cargo.toml"
mkdir -p "$OUT_DIR"
cp "$PROJECT_ROOT/capture/target/release/mnemosyne-capture" "$OUT"
chmod +x "$OUT"
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
