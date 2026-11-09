#!/bin/bash
# Download pinned static ffmpeg and ffprobe builds as Tauri sidecar binaries (macOS Apple silicon only).
# Linux uses the system ffmpeg, so there this script does nothing.
# Output: src-tauri/binaries/ffmpeg-<target-triple> and ffprobe-<target-triple>
# Source: https://ffmpeg.martin-riedl.de/ release builds (version below); the SHA-256 values are the ones
# the site publishes next to each zip (<url>.sha256), copied here so a changed download fails the build.
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "fetch-ffmpeg: not macOS arm64, nothing to do (Linux uses the system ffmpeg)"
  exit 0
fi

FFMPEG_VERSION="9.0.2"
BASE="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_$FFMPEG_VERSION"
FFMPEG_SHA256="c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"
FFPROBE_SHA256="fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_ROOT/src-tauri/binaries"
TRIPLE="aarch64-apple-darwin"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT_DIR"

fetch() { # <name> <expected sha256 of the zip>
  local name="$1" want="$2" out="$OUT_DIR/$1-$TRIPLE"
  if [ -x "$out" ] && "$out" -version 2>/dev/null | head -n 1 | grep -q "version $FFMPEG_VERSION"; then
    echo "$name $FFMPEG_VERSION sidecar already present: $out"
    return 0
  fi
  echo "Downloading $name $FFMPEG_VERSION for $TRIPLE..."
  curl -fsSL -o "$TMP/$name.zip" "$BASE/$name.zip"
  local got
  got="$(sha256_of "$TMP/$name.zip")"
  if [ "$got" != "$want" ]; then
    echo "$name checksum mismatch: expected $want, got $got" >&2
    exit 1
  fi
  mkdir -p "$TMP/$name"
  unzip -q -o "$TMP/$name.zip" -d "$TMP/$name"
  cp "$TMP/$name/$name" "$out"
  chmod +x "$out"
  # A downloaded file carries the quarantine flag; drop it so the build can run it.
  xattr -d com.apple.quarantine "$out" 2>/dev/null || true
  echo "Wrote $out ($("$out" -version | head -n 1))"
}

fetch ffmpeg "$FFMPEG_SHA256"
fetch ffprobe "$FFPROBE_SHA256"
