"""Manages the transcription engine and its GPU memory."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from ..transcription.engine import Transcriber, TranscriptionEngine

logger = logging.getLogger(__name__)


def rss_mb() -> float:
    """This process's resident memory, in MB (0 where /proc is missing)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def release_memory() -> None:
    """After models are dropped: collect their cycles, give CUDA's cached blocks back (only
    when torch is already loaded), and hand freed heap pages back to the system (glibc keeps
    them otherwise, so an idle backend stayed at ~7 GB)."""
    import ctypes
    import gc
    import sys

    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug("CUDA cache not emptied", exc_info=True)
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # not glibc
        pass


GPU_MODULES = {"torch", "torchaudio", "whisperx", "pyannote", "faster_whisper", "ctranslate2"}


class ModelService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine: TranscriptionEngine | None = None
        self._live: Transcriber | None = None
        self._live_embedder = None
        self._live_embedder_built = False
        self._live_rediarizer = None
        self._live_rediarizer_built = False
        self._live_stream = None
        self._live_stream_built = False
        self.last_used = time.monotonic()  # for unloading idle models (unload_if_idle)
        # One load at a time: setup's prepare job and a first transcription may both ask.
        self._load_lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        """Whether any model may be holding memory (GPU memory, on NVIDIA)."""
        return (
            (self._engine is not None and self._engine.is_loaded())
            or self._live is not None
            or self._live_embedder is not None
            or self._live_rediarizer is not None
            or (self._live_stream is not None and self._live_stream.is_loaded())
        )

    def touch(self) -> None:
        self.last_used = time.monotonic()

    async def unload_if_idle(self, busy: bool, now: float | None = None) -> bool:
        """Unload every model after `unload_models_after_minutes` without use, so games and
        other GPU work get the memory back; the next transcription loads them again. Never
        while `busy` (recording, or jobs running). Returns whether it unloaded."""
        minutes = self.settings.unload_models_after_minutes
        if busy:
            self.touch()
            return False
        if minutes <= 0 or not self.loaded:
            return False
        if (now if now is not None else time.monotonic()) - self.last_used < minutes * 60:
            return False
        before = rss_mb()
        await self.unload()
        release_memory()
        logger.info(
            "Unloaded the speech models after %d idle minutes (process %.0f -> %.0f MB)",
            minutes,
            before,
            rss_mb(),
        )
        return True

    @property
    def engine(self) -> TranscriptionEngine:
        self.touch()
        if self._engine is None:
            # Imported lazily so the API starts without torch.
            from ..transcription.registry import build_engine

            self._engine = build_engine(self.settings)
            logger.info("Built transcription engine: %s", getattr(self._engine, "name", "?"))
        return self._engine

    @property
    def live_transcriber(self) -> Transcriber:
        self.touch()
        if self._live is None:
            from ..transcription.registry import build_live_transcriber

            self._live = build_live_transcriber(self.settings)
            logger.info("Built live transcriber: %s", self._live.name)
        return self._live

    @property
    def live_embedder(self):
        """Speaker embedder for live labels (None if unavailable); built once."""
        if not self._live_embedder_built:
            from ..transcription.registry import build_live_embedder

            self._live_embedder = build_live_embedder(self.settings)
            self._live_embedder_built = True
        return self._live_embedder

    @property
    def live_rediarizer(self):
        """Diarizer for correcting live speaker labels (None when not wanted or available).
        Shares the final engine's Nemotron when that is already built."""
        if not self._live_rediarizer_built:
            from ..transcription.registry import build_live_rediarizer, resolve_live_diarizer

            engine_diarizer = getattr(self._engine, "diarizer", None)
            if not self.settings.live_diarization or self.settings.live_rediarize == "off":
                self._live_rediarizer = None
            elif resolve_live_diarizer(self.settings) == "streaming":
                self._live_rediarizer = None  # streamed sources have their speakers already
            elif getattr(engine_diarizer, "name", None) == "nemotron":
                self._live_rediarizer = engine_diarizer
            else:
                self._live_rediarizer = build_live_rediarizer(self.settings)
            self._live_rediarizer_built = True
        return self._live_rediarizer

    @property
    def live_stream_model(self):
        """Nemotron streaming for live speaker labels (None when not wanted or available);
        its own instance, never the engine's diarizer."""
        if not self._live_stream_built:
            from ..transcription.registry import build_live_stream_model

            self._live_stream = build_live_stream_model(self.settings)
            self._live_stream_built = True
        return self._live_stream

    async def ensure_loaded(
        self, on_download: Callable[[int, int], None] | None = None
    ) -> TranscriptionEngine:
        """The engine, loaded. `on_download(done_mb, total_mb)` hears about a first download of
        its models while it loads (services/model_downloads.py)."""
        async with self._load_lock:
            return await self._ensure_loaded(on_download)

    async def _ensure_loaded(
        self, on_download: Callable[[int, int], None] | None
    ) -> TranscriptionEngine:
        engine = self.engine
        if not engine.is_loaded():
            try:
                if on_download is None:
                    await engine.load()
                else:
                    from .model_downloads import hf_cache, speech_mb, watched

                    expected = await asyncio.to_thread(speech_mb, self.settings)
                    folders = [hf_cache(), self.settings.models_dir]
                    await watched(engine.load(), folders, expected, on_download)
            except ModuleNotFoundError as e:
                if e.name and e.name.split(".")[0] in GPU_MODULES:
                    # Release builds install the GPU extra in the background after the
                    # first start, and only when an NVIDIA driver is present.
                    self._engine = None
                    raise RuntimeError(
                        f"{self.settings.transcriber} needs GPU support ({e.name}), which is "
                        "not installed yet: it installs in the background on machines with an "
                        "NVIDIA driver. Use Parakeet (Settings -> Transcription) meanwhile."
                    ) from e
                raise
        return engine

    async def unload(self) -> None:
        if self._engine is not None:
            await self._engine.unload()
            self._engine = None
        await self.unload_live()

    async def unload_live(self) -> None:
        if self._live is not None:
            await self._live.unload()
            self._live = None
        if self._live_embedder is not None:
            await self._live_embedder.unload()
        self._live_embedder = None
        self._live_embedder_built = False
        engine_diarizer = getattr(self._engine, "diarizer", None)
        if self._live_rediarizer is not None and self._live_rediarizer is not engine_diarizer:
            await self._live_rediarizer.unload()
        self._live_rediarizer = None
        self._live_rediarizer_built = False
        # A recording that is streaming keeps its instance (settings changed mid-meeting); it
        # is freed with that recording's live job.
        if self._live_stream is not None and not self._live_stream.in_use():
            await self._live_stream.unload()
        self._live_stream = None
        self._live_stream_built = False

    async def apply_settings(self, settings: Settings) -> None:
        """Adopt new settings; drop the engine if anything it was built from changed."""
        from ..transcription.registry import ENGINE_SETTINGS, LIVE_SETTINGS

        old, self.settings = self.settings, settings
        if any(getattr(old, f) != getattr(settings, f) for f in ENGINE_SETTINGS):
            if self._engine is not None:
                await self._engine.unload()
                self._engine = None
        if any(getattr(old, f) != getattr(settings, f) for f in LIVE_SETTINGS):
            await self.unload_live()
