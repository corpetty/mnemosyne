"""Manages the transcription engine and its GPU memory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from ..transcription.engine import TranscriptionEngine

logger = logging.getLogger(__name__)


class ModelService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine: TranscriptionEngine | None = None

    @property
    def engine(self) -> TranscriptionEngine:
        if self._engine is None:
            # Imported lazily so the API starts without torch.
            from ..transcription.whisperx_engine import WhisperXEngine

            s = self.settings
            self._engine = WhisperXEngine(
                model_size=s.whisper_model_size,
                compute_type=s.whisper_compute_type,
                batch_size=s.whisper_batch_size,
                hf_token=s.hf_token,
            )
        return self._engine

    async def ensure_loaded(self) -> TranscriptionEngine:
        engine = self.engine
        if not engine.is_loaded():
            await engine.load()
        return engine

    async def unload(self) -> None:
        if self._engine is not None:
            await self._engine.unload()
            self._engine = None

    async def apply_settings(self, settings: Settings) -> None:
        """Adopt new settings; drop the loaded engine if its config changed."""
        old, self.settings = self.settings, settings
        changed = any(
            getattr(old, f) != getattr(settings, f)
            for f in (
                "whisper_model_size",
                "whisper_compute_type",
                "whisper_batch_size",
                "hf_token",
            )
        )
        if changed:
            await self.unload()
