"""Manages the transcription engine and its GPU memory."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from ..transcription.engine import Transcriber, TranscriptionEngine

logger = logging.getLogger(__name__)

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
        await self.unload()
        logger.info("Unloaded the speech models after %d idle minutes", minutes)
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

    async def ensure_loaded(self) -> TranscriptionEngine:
        engine = self.engine
        if not engine.is_loaded():
            try:
                await engine.load()
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
