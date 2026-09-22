"""Engine built from a Transcriber and a Diarizer."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from ..models.transcript import TranscriptSegment
from .assign import assign_speakers, relabel
from .dedup import remove_echo
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
        echo_dedup: bool = True,
        echo_similarity: float = 0.8,
    ):
        self.transcriber = transcriber
        self.diarizer = diarizer
        self.language = language or None
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers
        self.echo_dedup = echo_dedup
        self.echo_similarity = echo_similarity
        self.last_speaker_embeddings: dict[str, list[float]] = {}
        self.last_dropped_echo: int = 0

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

    async def transcribe_source(
        self, source: AudioSource
    ) -> tuple[list[TranscriptSegment], dict[str, list[float]]]:
        segments = await self.transcriber.transcribe(source.path, language=self.language)
        if source.speaker_label is not None:
            return relabel(segments, source.speaker_label), {}
        if self.diarizer is None:
            return relabel(segments, "SPEAKER_00"), {}
        result = await self.diarizer.diarize(
            source.path, min_speakers=self.min_speakers, max_speakers=self.max_speakers
        )
        logger.info(
            "Diarized %s: %d turns, speakers=%s",
            source.path,
            len(result.turns),
            sorted({t.speaker for t in result.turns}),
        )
        return assign_speakers(segments, result.turns), dict(result.embeddings)

    async def transcribe_sources(
        self, sources: list[AudioSource]
    ) -> AsyncIterator[TranscriptSegment]:
        """Transcribe every source, then yield all segments merged by start time.

        With a labelled mic source and unlabelled (diarized) sources, mic
        segments that repeat the diarized audio are treated as speaker bleed
        and dropped (see transcription/dedup.py).
        """
        if not self.is_loaded():
            await self.load()
        self.last_speaker_embeddings = {}
        self.last_dropped_echo = 0

        labelled: list[TranscriptSegment] = []
        diarized: list[TranscriptSegment] = []
        for source in sources:
            logger.info("Transcribing %s source: %s", source.kind, source.path)
            segments, embeddings = await self.transcribe_source(source)
            self.last_speaker_embeddings.update(embeddings)
            (labelled if source.speaker_label is not None else diarized).extend(segments)

        if self.echo_dedup and labelled and diarized:
            labelled, dropped = remove_echo(labelled, diarized, threshold=self.echo_similarity)
            self.last_dropped_echo = len(dropped)
            if dropped:
                logger.info("Dropped %d mic segment(s) as speaker bleed", len(dropped))

        collected = labelled + diarized
        collected.sort(key=lambda s: (s.start, s.end))
        for seg in collected:
            yield seg
