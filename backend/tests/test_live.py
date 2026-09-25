"""Live transcription: WAV tailing, commit logic, job start/stop."""

import wave
from pathlib import Path

import numpy as np
import pytest

from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.transcription.live import LiveSource, LiveTranscriber, WavTail

RATE = 48000


def _open_wav(path: Path):
    w = wave.open(str(path), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    return w


def _append_seconds(path: Path, seconds: float, value: int = 1000):
    """Append PCM to a WAV without rewriting the header (like a live recorder)."""
    with path.open("ab") as f:
        f.write(np.full(int(RATE * seconds), value, dtype=np.int16).tobytes())


@pytest.fixture
def growing_wav(tmp_path):
    path = tmp_path / "live.wav"
    with _open_wav(path) as w:
        w.writeframes(np.zeros(RATE // 2, dtype=np.int16).tobytes())  # 0.5 s
    return path


def test_wav_tail_reads_incrementally(growing_wav):
    tail = WavTail(growing_wav)
    first = tail.read_new()
    assert tail.sample_rate == RATE and tail.channels == 1
    assert first.size == RATE // 2
    assert tail.read_new().size == 0
    _append_seconds(growing_wav, 1.0, value=7)
    more = tail.read_new()
    assert more.size == RATE and more[0] == 7
    assert tail.read_new().size == 0


def test_wav_tail_missing_file_is_empty(tmp_path):
    assert WavTail(tmp_path / "nope.wav").read_new().size == 0


class DurationTranscriber:
    """Fake: one segment per whole second of audio [i, i+0.8] with text `w<i>`."""

    name = "fake-live"

    def __init__(self):
        self.loaded = False
        self.calls = 0

    def is_loaded(self):
        return self.loaded

    async def load(self):
        self.loaded = True

    async def unload(self):
        self.loaded = False

    async def transcribe(self, path, language=None):
        self.calls += 1
        with wave.open(path, "rb") as w:
            seconds = w.getnframes() / w.getframerate()
        return [
            TranscriptSegment(text=f"w{i}", speaker="UNKNOWN", start=i, end=i + 0.8)
            for i in range(int(seconds))
        ]


@pytest.mark.anyio
async def test_live_commits_before_margin_and_advances(growing_wav):
    events = []
    source = LiveSource(path=growing_wav, speaker="Me", kind="mic")
    live = LiveTranscriber(
        DurationTranscriber(), [source], events.append, "s1", commit_margin=1.0, min_buffer=1.0
    )
    await live.transcriber.load()

    _append_seconds(growing_wav, 2.5)  # buffer = 3.0 s -> segments w0,w1,w2; cutoff 2.0
    await live.tick()
    committed = [e for e in events if e["type"] == "live_segment"]
    assert [e["segment"]["text"] for e in committed] == ["w0", "w1"]
    assert committed[0]["segment"]["speaker"] == "Me"
    assert committed[1]["segment"]["start"] == 1.0
    partial = [e for e in events if e["type"] == "live_partial"]
    assert partial[-1]["text"] == "w2"
    # buffer cut at 1.8 s -> 1.2 s left, starting at 1.8
    assert abs(source.buffer_start - 1.8) < 1e-6
    assert abs(source.buffer.size / RATE - 1.2) < 1e-3

    _append_seconds(growing_wav, 2.0)  # buffer 3.2 s (starts at 1.8) -> w0..w2 relative
    events.clear()
    await live.tick()
    committed = [e["segment"] for e in events if e["type"] == "live_segment"]
    assert [c["text"] for c in committed] == ["w0", "w1"]
    assert committed[0]["start"] == 1.8 and committed[1]["start"] == 2.8

    events.clear()
    await live.tick(flush=True)  # commits everything left
    assert [e["segment"]["text"] for e in events if e["type"] == "live_segment"] == ["w0"]
    assert [e for e in events if e["type"] == "live_partial"][-1]["text"] == ""
    assert len(live.committed) == 5


@pytest.mark.anyio
async def test_live_skips_short_buffers(growing_wav):
    events = []
    t = DurationTranscriber()
    live = LiveTranscriber(t, [LiveSource(path=growing_wav, speaker="S")], events.append, "s1")
    await live.tick()  # 0.5 s < min_buffer
    assert t.calls == 0 and events == []


def test_recording_starts_and_stops_live_job(client, ctx, fake_pipewire, fake_engine):
    started = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()
    assert started["live_job_id"]
    job = client.get(f"/api/jobs/{started['live_job_id']}").json()
    assert job["kind"] == "live" and job["status"] in ("queued", "running")

    from tests.conftest import drain_until_job

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        stopped = client.post(f"/api/audio/stop/{started['session_id']}").json()
        drain_until_job(ws, stopped["job_id"])
    live = client.get(f"/api/jobs/{started['live_job_id']}").json()
    assert live["status"] == "completed"


def test_live_disabled_by_setting(client, ctx, fake_pipewire):
    ctx.settings.live_transcription = False
    started = client.post("/api/audio/start", json={"device_ids": [1]}).json()
    assert started["live_job_id"] is None


@pytest.mark.anyio
async def test_silent_audio_is_not_transcribed(growing_wav):
    """Digital silence (EasyEffects between sentences) never reaches the transcriber."""
    events = []
    t = DurationTranscriber()
    source = LiveSource(path=growing_wav, speaker="Me", kind="mic")
    live = LiveTranscriber(t, [source], events.append, "s1", commit_margin=1.0)
    await live.transcriber.load()
    _append_seconds(growing_wav, 5.0, value=0)
    await live.tick()
    assert t.calls == 0 and events == []
    assert source.buffer.size / RATE == pytest.approx(1.0)  # only the margin is kept
    assert source.buffer_start == pytest.approx(4.5)  # 0.5 s header region + 5 s - 1 s
    _append_seconds(growing_wav, 3.0, value=1000)  # someone speaks
    await live.tick()
    assert t.calls == 1


def test_parakeet_live_thread_budget():
    from mnemosyne.config import Settings
    from mnemosyne.transcription.registry import build_live_transcriber, build_transcriber

    st = Settings(live_transcriber="parakeet", live_threads=2, transcriber="parakeet")
    assert build_live_transcriber(st).threads == 2
    assert build_transcriber(st).threads is None  # final transcription may use every core
