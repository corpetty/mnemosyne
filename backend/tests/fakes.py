"""Test doubles for the ML engine and LLM providers.

These implement the same Protocols as the real implementations
(`transcription.engine.TranscriptionEngine`, `summarization.provider.SummarizationProvider`)
so the API and services can be exercised without torch, CUDA, or network access.
"""

from collections.abc import AsyncIterator

from src.mnemosyne.models.transcript import TranscriptSegment

FAKE_SEGMENTS = [
    TranscriptSegment(text="Hello everyone.", speaker="SPEAKER_00", start=0.0, end=1.2),
    TranscriptSegment(text="Hi, thanks for joining.", speaker="SPEAKER_01", start=1.5, end=3.0),
    TranscriptSegment(text="Let's get started.", speaker="SPEAKER_00", start=3.2, end=4.5),
]


class FakeEngine:
    """TranscriptionEngine that yields canned segments without loading any model."""

    def __init__(self, segments: list[TranscriptSegment] | None = None, fail: bool = False):
        self.segments = segments if segments is not None else FAKE_SEGMENTS
        self.fail = fail
        self.loaded = False
        self.transcribed_paths: list[str] = []

    def is_loaded(self) -> bool:
        return self.loaded

    async def load(self) -> None:
        self.loaded = True

    async def unload(self) -> None:
        self.loaded = False

    async def transcribe(self, audio_path: str) -> AsyncIterator[TranscriptSegment]:
        self.transcribed_paths.append(audio_path)
        if self.fail:
            raise RuntimeError("fake engine failure")
        for seg in self.segments:
            yield seg


class FakeProvider:
    """SummarizationProvider that records calls and returns a fixed summary."""

    name = "fake"

    def __init__(self, models: list[str] | None = None, summary: str = "## Summary\n- fake"):
        self.models = models if models is not None else ["fake-model-a", "fake-model-b"]
        self.summary = summary
        self.calls: list[dict] = []

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def summarize(self, transcript: str, model: str, system_prompt: str) -> str:
        self.calls.append(
            {"transcript": transcript, "model": model, "system_prompt": system_prompt}
        )
        return self.summary
