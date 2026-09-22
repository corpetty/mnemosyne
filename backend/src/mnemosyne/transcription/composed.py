"""Engine built from a Transcriber and a Diarizer."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..models.transcript import TranscriptSegment
from .assign import assign_speakers, relabel
from .engine import AudioSource, Diarizer, Transcriber

logger = logging.getLogger(__name__)


class ComposedEngine:
    def __init__(
        self,
        transcriber: Transcriber,
        diarizer: Diarizer | None,
        language: str | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ):
        self.transcriber = transcriber
        self.diarizer = diarizer
        self.language = language or None
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    @property
    def name(self) -> str:
        d = self.diarizer.name if self.diarizer else "none"
        return f"{self.transcriber.name}+{d}"

    def is_loaded(self) -> bool:
        if not self.transcriber.is_loaded():
            return False
        return self.diarizer is None or self.diarizer.is_loaded()

    async def load(self) -> None:
        if not self.transcriber.is_loaded():
            await self.transcriber.load()
        if self.diarizer is not None and not self.diarizer.is_loaded():
            await self.diarizer.load()

    async def unload(self) -> None:
        await self.transcriber.unload()
        if self.diarizer is not None:
            await self.diarizer.unload()

    async def transcribe_source(self, source: AudioSource) -> list[TranscriptSegment]:
        segments = await self.transcriber.transcribe(source.path, language=self.language)
        if source.speaker_label is not None:
            return relabel(segments, source.speaker_label)
        if self.diarizer is None:
            return relabel(segments, "SPEAKER_00")
        turns = await self.diarizer.diarize(
            source.path, min_speakers=self.min_speakers, max_speakers=self.max_speakers
        )
        logger.info(
            "Diarized %s: %d turns, speakers=%s",
            source.path,
            len(turns),
            sorted({t.speaker for t in turns}),
        )
        return assign_speakers(segments, turns)

    async def transcribe_sources(
        self, sources: list[AudioSource]
    ) -> AsyncIterator[TranscriptSegment]:
        """Transcribe every source, then yield all segments merged by start time.

        Sources are processed sequentially; each one's segments are fully
        available only when it completes, so the merge happens at the end.
        """
        if not self.is_loaded():
            await self.load()
        collected: list[TranscriptSegment] = []
        for source in sources:
            logger.info("Transcribing %s source: %s", source.kind, source.path)
            collected.extend(await self.transcribe_source(source))
        collected.sort(key=lambda s: (s.start, s.end))
        for seg in collected:
            yield seg
