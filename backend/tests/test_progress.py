"""Progress weighting across sources and stages in ComposedEngine."""

import pytest

from mnemosyne.transcription import composed
from mnemosyne.transcription.composed import ComposedEngine, source_weights
from mnemosyne.transcription.engine import AudioSource
from tests.fakes import FakeDiarizer, FakeTranscriber


def test_source_weights(monkeypatch):
    srcs = [AudioSource(path="/a"), AudioSource(path="/b")]
    monkeypatch.setattr(composed, "audio_duration", lambda p: {"/a": 30.0, "/b": 90.0}[p])
    assert source_weights(srcs) == [0.25, 0.75]
    monkeypatch.setattr(composed, "audio_duration", lambda p: None)
    assert source_weights(srcs) == [0.5, 0.5]
    assert source_weights([]) == []


class SteppingTranscriber(FakeTranscriber):
    async def transcribe(self, audio_path, language=None, progress=None):
        for f in (0.25, 0.5, 1.0):
            progress(f)
        return await super().transcribe(audio_path, language)


@pytest.mark.anyio
async def test_progress_is_monotonic_and_weighted(monkeypatch):
    import asyncio

    monkeypatch.setattr(composed, "audio_duration", lambda p: {"/mic": 10.0, "/sys": 30.0}[p])
    seen = []
    engine = ComposedEngine(SteppingTranscriber(), FakeDiarizer(), echo_dedup=False)
    sources = [
        AudioSource(path="/mic", kind="mic", speaker_label="Me"),
        AudioSource(path="/sys", kind="system"),
    ]
    _ = [
        s
        async for s in engine.transcribe_sources(
            sources, on_progress=lambda st, f: seen.append((st, f))
        )
    ]
    await asyncio.sleep(0)  # let call_soon_threadsafe callbacks run
    fracs = [f for _, f in seen]
    assert fracs == sorted(fracs)
    assert fracs[-1] == pytest.approx(1.0)
    # mic (25 % of the audio, transcription only) finishes at 0.25
    assert ("Transcribing mic audio", pytest.approx(0.25)) in seen
    # system: transcription is 70 % of its 75 % share, then diarization
    assert ("Transcribing system audio", pytest.approx(0.25 + 0.75 * 0.7)) in seen
    assert seen[-1][0] == "Identifying speakers in system audio"
