"""Manages the transcription engine and its GPU memory."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:
    from ..transcription.engine import Transcriber, TranscriptionEngine

logger = logging.getLogger(__name__)


class ModelService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine: TranscriptionEngine | None = None
        self._live: Transcriber | None = None

    @property
    def engine(self) -> TranscriptionEngine:
        if self._engine is None:
            # Imported lazily so the API starts without torch.
            from ..transcription.registry import build_engine

            self._engine = build_engine(self.settings)
            logger.info("Built transcription engine: %s", getattr(self._engine, "name", "?"))
        return self._engine

    @property
    def live_transcriber(self) -> Transcriber:
        if self._live is None:
            from ..transcription.registry import build_live_transcriber

            self._live = build_live_transcriber(self.settings)
            logger.info("Built live transcriber: %s", self._live.name)
        return self._live

    async def ensure_loaded(self) -> TranscriptionEngine:
        engine = self.engine
        if not engine.is_loaded():
            await engine.load()
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
