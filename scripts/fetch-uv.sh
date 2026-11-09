#!/bin/bash
# Download the pinned uv release as a Tauri sidecar binary.
# Output: src-tauri/binaries/mnemosyne-uv-<target-triple>
set -euo pipefail

UV_VERSION="${UV_VERSION:-0.10.4}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_ROOT/src-tauri/binaries"

TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"
OUT="$OUT_DIR/mnemosyne-uv-$TRIPLE"

if [ -x "$OUT" ] && "$OUT" --version 2>/dev/null | grep -q " $UV_VERSION"; then
  echo "uv $UV_VERSION sidecar already present: $OUT"
  exit 0
fi

ASSET="uv-$TRIPLE.tar.gz"
BASE="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading uv $UV_VERSION for $TRIPLE..."
curl -fsSL -o "$TMP/$ASSET" "$BASE/$ASSET"
curl -fsSL -o "$TMP/$ASSET.sha256" "$BASE/$ASSET.sha256"
# sha256sum is GNU; macOS ships shasum.
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$TMP/$ASSET" | cut -d' ' -f1)"
else
  ACTUAL="$(shasum -a 256 "$TMP/$ASSET" | cut -d' ' -f1)"
fi
EXPECTED="$(cut -d' ' -f1 "$TMP/$ASSET.sha256")"
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "uv checksum mismatch for $ASSET: expected $EXPECTED, got $ACTUAL" >&2
  exit 1
fi
tar -xzf "$TMP/$ASSET" -C "$TMP"

mkdir -p "$OUT_DIR"
cp "$TMP/uv-$TRIPLE/uv" "$OUT"
chmod +x "$OUT"
echo "Wrote $OUT ($("$OUT" --version))"
