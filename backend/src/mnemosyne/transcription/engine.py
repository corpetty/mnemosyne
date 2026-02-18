"""Transcription engine protocol."""

from typing import Protocol, AsyncIterator

from ..models.transcript import TranscriptSegment


class TranscriptionEngine(Protocol):
    """Interface for transcription + diarization engines."""

    async def transcribe(self, audio_path: str) -> AsyncIterator[TranscriptSegment]:
        """Transcribe audio file, yielding segments as they complete."""
        ...

    def is_loaded(self) -> bool:
        """Whether the ML models are loaded in memory."""
        ...

    async def load(self) -> None:
        """Load ML models into GPU memory."""
        ...

    async def unload(self) -> None:
        """Unload ML models and free GPU memory."""
        ...
