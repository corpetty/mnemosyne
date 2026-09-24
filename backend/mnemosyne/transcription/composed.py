"""Engine built from a Transcriber and a Diarizer."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator, Callable

from ..models.transcript import TranscriptSegment
from .assign import assign_speakers, relabel
from .dedup import remove_echo
from .engine import AudioSource, Diarizer, StageProgressFn, Transcriber

logger = logging.getLogger(__name__)


def audio_duration(path: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def source_weights(sources: list[AudioSource]) -> list[float]:
    """Each source's share of the total work, by audio duration (equal if unknown)."""
    durations = [audio_duration(s.path) for s in sources]
    if not sources:
        return []
    if any(d is None or d <= 0 for d in durations):
        return [1.0 / len(sources)] * len(sources)
    total = sum(durations)
    return [d / total for d in durations]


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
        self, source: AudioSource, report: Callable[[str, float], None] | None = None
    ) -> tuple[list[TranscriptSegment], dict[str, list[float]]]:
        """Transcribe (and maybe diarize) one source. `report(stage, fraction)` gets this
        source's own 0..1 progress."""
        diarize = source.speaker_label is None and self.diarizer is not None
        share = 0.7 if diarize else 1.0
        kind = "" if source.kind == "mixed" else f"{source.kind} "

        def t_progress(f: float) -> None:
            if report:
                report(f"Transcribing {kind}audio", share * f)

        segments = await self.transcriber.transcribe(
            source.path, language=self.language, progress=t_progress
        )
        if source.speaker_label is not None:
            return relabel(segments, source.speaker_label), {}
        if self.diarizer is None:
            return relabel(segments, "SPEAKER_00"), {}

        def d_progress(f: float) -> None:
            if report:
                report(f"Identifying speakers in {kind}audio", share + (1 - share) * f)

        result = await self.diarizer.diarize(
            source.path,
            min_speakers=self.min_speakers,
            max_speakers=self.max_speakers,
            progress=d_progress,
        )
        logger.info(
            "Diarized %s: %d turns, speakers=%s",
            source.path,
            len(result.turns),
            sorted({t.speaker for t in result.turns}),
        )
        return assign_speakers(segments, result.turns), dict(result.embeddings)

    async def transcribe_sources(
        self, sources: list[AudioSource], on_progress: StageProgressFn | None = None
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
        weights = source_weights(sources)
        loop = asyncio.get_running_loop()
        done = 0.0
        for source, weight in zip(sources, weights, strict=True):
            logger.info("Transcribing %s source: %s", source.kind, source.path)

            def report(stage: str, frac: float, base=done, weight=weight) -> None:
                # Engines call this from worker threads; hop back to the event loop.
                if on_progress is not None:
                    loop.call_soon_threadsafe(on_progress, stage, base + weight * frac)

            segments, embeddings = await self.transcribe_source(source, report)
            done += weight
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
