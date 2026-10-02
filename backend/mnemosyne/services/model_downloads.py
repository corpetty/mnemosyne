"""What the speech and search models cost to download, and watching a download happen.

The engines fetch their own models when they first load (Hugging Face's cache, or `models_dir`
for the onnx diarizer). To say how far along that is, a DownloadWatch adds up how much those
folders grew while a load runs, against the expected size of what is being loaded.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from ..config import Settings

MB = 1_000_000

# Expected first-download sizes (the model files, rounded up a little).
TRANSCRIBER_MB = {"parakeet": 690, "remote": 0, "demo": 0}
WHISPER_MB = {
    "tiny": 80,
    "tiny.en": 80,
    "base": 150,
    "base.en": 150,
    "small": 490,
    "small.en": 490,
    "medium": 1530,
    "medium.en": 1530,
    "large-v2": 3100,
    "large-v3": 3100,
    "large-v3-turbo": 1620,
    "turbo": 1620,
}
DIARIZER_MB = {"nemotron": 190, "pyannote": 35, "onnx": 45, "none": 0, "demo": 0}
SEARCH_MB = 130  # potion-retrieval-32M


def hf_cache() -> Path:
    """Where Hugging Face downloads go (the same rules as huggingface_hub)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "huggingface"
    )
    return Path(home) / "hub"


def speech_mb(settings: Settings) -> int:
    """About how many MB the chosen transcriber and diarizer download on first use."""
    from ..transcription.registry import resolve_diarizer, resolve_transcriber

    transcriber = resolve_transcriber(settings)
    if transcriber == "whisperx":
        mb = WHISPER_MB.get(settings.whisper_model_size, 1530)
    else:
        mb = TRANSCRIBER_MB.get(transcriber, 0)
    return mb + DIARIZER_MB.get(resolve_diarizer(settings), 0)


def folder_bytes(folders: list[Path]) -> int:
    """Bytes in regular files under these folders (symlinks not counted twice)."""
    total = 0
    for folder in folders:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    st = os.lstat(os.path.join(root, name))
                except OSError:
                    continue
                if not os.path.islink(os.path.join(root, name)):
                    total += st.st_size
    return total


T = TypeVar("T")


async def watched(
    work: Awaitable[T],
    folders: list[Path],
    expected_mb: int,
    on_progress: Callable[[int, int], None],
    interval: float = 0.5,
    min_bytes: int = 2 * MB,
) -> T:
    """Run `work`, calling on_progress(done_mb, total_mb) while the folders grow by more than
    `min_bytes` (a model being downloaded, not one already there being opened)."""
    start = await asyncio.to_thread(folder_bytes, folders)
    task = asyncio.ensure_future(work)
    while not task.done():
        await asyncio.wait({task}, timeout=interval)
        grown = await asyncio.to_thread(folder_bytes, folders) - start
        if grown > min_bytes and not task.done():
            done = grown // MB
            on_progress(done, max(expected_mb, round(done * 1.05)))
    return task.result()
