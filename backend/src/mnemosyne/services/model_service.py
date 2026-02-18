"""Singleton service for managing ML model lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import config

if TYPE_CHECKING:
    from ..transcription.whisperx_engine import WhisperXEngine

logger = logging.getLogger(__name__)


class ModelService:
    """Manages ML model instances and their GPU memory."""

    def __init__(self):
        self._engine: WhisperXEngine | None = None

    @property
    def engine(self) -> WhisperXEngine:
        if self._engine is None:
            from ..transcription.whisperx_engine import WhisperXEngine

            self._engine = WhisperXEngine(
                model_size=config.WHISPER_MODEL_SIZE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
                batch_size=config.WHISPER_BATCH_SIZE,
                hf_token=config.HF_TOKEN,
            )
        return self._engine

    async def ensure_loaded(self) -> WhisperXEngine:
        """Ensure the transcription engine is loaded and return it."""
        engine = self.engine
        if not engine.is_loaded():
            await engine.load()
        return engine

    async def unload(self) -> None:
        """Unload all models and free GPU memory."""
        if self._engine is not None:
            await self._engine.unload()
            self._engine = None


# Global singleton
model_service = ModelService()
