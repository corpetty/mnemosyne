#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"
TAURI_DIR="$PROJECT_ROOT/src-tauri"
SIDECAR_NAME="mnemosyne-backend"
OUTPUT_DIR="$TAURI_DIR/binaries"

echo "=== Building Mnemosyne Backend Sidecar ==="
echo "Backend dir: $BACKEND_DIR"

cd "$BACKEND_DIR"

# Ensure core deps are synced
echo "Syncing core dependencies..."
uv sync

# Install PyInstaller if not present
uv pip install pyinstaller 2>/dev/null || true

# Ensure ML deps are installed
echo "Checking ML dependencies..."
uv pip install torch torchaudio 2>/dev/null || true
uv pip install whisperx 2>/dev/null || true

echo "Running PyInstaller (--onedir)..."
uv run pyinstaller \
    --name "$SIDECAR_NAME" \
    --onedir \
    --noconfirm \
    --clean \
    --collect-all torch \
    --collect-all torchaudio \
    --collect-all whisperx \
    --collect-all faster_whisper \
    --collect-all ctranslate2 \
    --collect-all pyannote \
    --collect-all lightning_fabric \
    --collect-all speechbrain \
    --collect-all asteroid_filterbanks \
    --hidden-import "torch._C" \
    --hidden-import "torch._C._jit" \
    --hidden-import "torch._C._nvrtc" \
    --hidden-import "uvicorn.logging" \
    --hidden-import "uvicorn.loops" \
    --hidden-import "uvicorn.loops.auto" \
    --hidden-import "uvicorn.protocols" \
    --hidden-import "uvicorn.protocols.http" \
    --hidden-import "uvicorn.protocols.http.auto" \
    --hidden-import "uvicorn.protocols.websockets" \
    --hidden-import "uvicorn.protocols.websockets.auto" \
    --hidden-import "uvicorn.lifespan" \
    --hidden-import "uvicorn.lifespan.on" \
    --hidden-import "uvicorn.lifespan.off" \
    --copy-metadata torch \
    --copy-metadata torchaudio \
    --copy-metadata faster-whisper \
    --copy-metadata whisperx \
    --add-data "src:src" \
    main.py

# Copy to Tauri binaries directory
echo "Copying sidecar to Tauri binaries..."
rm -rf "$OUTPUT_DIR/${SIDECAR_NAME}-dir"
mkdir -p "$OUTPUT_DIR"
cp -a "$BACKEND_DIR/dist/$SIDECAR_NAME" "$OUTPUT_DIR/${SIDECAR_NAME}-dir"

echo ""
echo "=== Backend sidecar built successfully ==="
echo "Output: $OUTPUT_DIR/${SIDECAR_NAME}-dir/"
du -sh "$OUTPUT_DIR/${SIDECAR_NAME}-dir/"
