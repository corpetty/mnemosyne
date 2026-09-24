"""Demo mode (used by the browser tests): builds from settings and speaks every prompt type."""

import pytest

from mnemosyne import demo
from mnemosyne.config import Settings
from mnemosyne.services.summarization_service import SummarizationService
from mnemosyne.summarization.prompts import get_system_prompt, parse_summary_response
from mnemosyne.transcription.registry import build_engine


@pytest.mark.anyio
async def test_demo_engine_yields_two_speakers(tmp_path):
    engine = build_engine(Settings(transcriber="demo", diarizer="demo"))
    from mnemosyne.transcription.engine import AudioSource

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    segs = [s async for s in engine.transcribe_sources([AudioSource(path=str(audio))])]
    assert [s.text for s in segs] == [t for *_, t in demo.SCRIPT]
    assert {s.speaker for s in segs} == {"SPEAKER_00", "SPEAKER_01"}


@pytest.mark.anyio
async def test_demo_provider_only_when_enabled(monkeypatch):
    assert "demo" not in SummarizationService(Settings()).providers
    monkeypatch.setenv("MNEMOSYNE_DEMO", "1")
    service = SummarizationService(Settings())
    assert list(service.providers) == ["demo"]
    raw = await service.providers["demo"].summarize("x", "demo-model", get_system_prompt(3))
    summary, data = parse_summary_response(raw)
    assert data.title == "Release planning"
    assert data.decisions == ["Ship the Waku migration in October"]
    assert data.action_items[0].owner == "SPEAKER_01"
