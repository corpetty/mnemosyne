#!/usr/bin/env python3
"""Check that the backend's `gpu` and `onnx` extras install and import, without a GPU.

Run from backend/ after `uv sync --locked --extra gpu --extra onnx`:
    uv run --no-sync python ../scripts/check-gpu-extra.py

Imports everything the GPU transcription and diarization code imports, and builds (does not
load) each transcriber and diarizer, so a broken pin or a NeMo tarball that no longer builds
fails CI instead of a user's first launch. Models are not downloaded.
"""

import importlib
import importlib.metadata
import sys

MODULES = ["torch", "torchaudio", "whisperx", "pyannote.audio", "nemo.collections.asr", "onnx_asr"]
DISTS = ["torch", "whisperx", "pyannote.audio", "nemo-toolkit", "lightning", "onnx-asr"]


def main() -> int:
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
            print(f"import {name}: ok")
        except Exception as e:  # noqa: BLE001 - report every failure, not just the first
            failed.append(name)
            print(f"import {name}: FAILED {e.__class__.__name__}: {e}")
    for dist in DISTS:
        try:
            print(f"{dist} {importlib.metadata.version(dist)}")
        except importlib.metadata.PackageNotFoundError:
            failed.append(dist)
            print(f"{dist}: NOT INSTALLED")

    from mnemosyne.config import Settings
    from mnemosyne.transcription.registry import build_diarizer, build_transcriber

    for transcriber in ("whisperx", "parakeet"):
        try:
            t = build_transcriber(Settings(transcriber=transcriber))
            print(f"transcriber {transcriber}: {type(t).__name__}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"transcriber {transcriber}")
            print(f"transcriber {transcriber}: FAILED {e}")
    for diarizer in ("pyannote", "nemotron", "auto"):
        try:
            d = build_diarizer(Settings(diarizer=diarizer))
            print(f"diarizer {diarizer}: {type(d).__name__}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"diarizer {diarizer}")
            print(f"diarizer {diarizer}: FAILED {e}")

    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("gpu extra OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
