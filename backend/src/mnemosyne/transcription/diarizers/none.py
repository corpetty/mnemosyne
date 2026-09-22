"""No-op diarizer: every segment becomes SPEAKER_00."""

from __future__ import annotations

from ..engine import DiarizationResult


class NoDiarizer:
    name = "none"

    def is_loaded(self) -> bool:
        return True

    async def load(self) -> None:
        pass

    async def unload(self) -> None:
        pass

    async def diarize(
        self,
        audio_path: str,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> DiarizationResult:
        return DiarizationResult(turns=[])
