"""Transcription interfaces.

The work is split into two independently pluggable stages:

- `Transcriber`: audio -> text segments with timestamps (and words when available).
- `Diarizer`:    audio -> speaker turns.

`TranscriptionEngine` is what the pipeline consumes. `ComposedEngine` builds one
from a Transcriber and a Diarizer and handles per-source speaker labelling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..models.base import ApiModel
from ..models.transcript import TranscriptSegment

# Called with a 0..1 fraction of the current call's work. May be called from a worker
# thread; ComposedEngine marshals it back to the event loop.
ProgressFn = Callable[[float], None]
# Engine-level progress: (stage description, overall 0..1 fraction).
StageProgressFn = Callable[[str, float], None]


class SpeakerTurn(ApiModel):
    start: float
    end: float
    speaker: str


class DiarizationResult(ApiModel):
    turns: list[SpeakerTurn]
    # One embedding vector per speaker label, when the diarizer can produce them.
    embeddings: dict[str, list[float]] = {}


SourceKind = Literal["mic", "system", "mixed"]


@dataclass(frozen=True)
class AudioSource:
    """One audio file to transcribe.

    `speaker_label` set means "everything in this file is this speaker" and the
    diarizer is skipped for it (the local mic when system audio is captured
    separately). Otherwise the file is diarized.
    """

    path: str
    kind: SourceKind = "mixed"
    speaker_label: str | None = None


class _Loadable(Protocol):
    name: str

    def is_loaded(self) -> bool: ...

    async def load(self) -> None: ...

    async def unload(self) -> None: ...


@runtime_checkable
class Transcriber(_Loadable, Protocol):
    async def transcribe(
        self, audio_path: str, language: str | None = None, progress: ProgressFn | None = None
    ) -> list[TranscriptSegment]:
        """Return time-ordered segments. `speaker` is left as "UNKNOWN"."""
        ...


@runtime_checkable
class Diarizer(_Loadable, Protocol):
    async def diarize(
        self,
        audio_path: str,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        progress: ProgressFn | None = None,
    ) -> DiarizationResult: ...


@runtime_checkable
class TranscriptionEngine(Protocol):
    """What the pipeline stage drives."""

    def is_loaded(self) -> bool: ...

    async def load(self) -> None: ...

    async def unload(self) -> None: ...

    def transcribe_sources(
        self, sources: list[AudioSource], on_progress: StageProgressFn | None = None
    ) -> AsyncIterator[TranscriptSegment]:
        """Transcribe one or more sources, yielding segments in time order.

        After the iterator is exhausted, `last_speaker_embeddings` (if the engine
        defines it) holds {speaker_label: vector} for the diarized speakers.
        """
        ...
