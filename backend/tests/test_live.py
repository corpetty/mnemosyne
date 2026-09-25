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


def _adaptive(tmp_path, pressure=lambda: None, interval=5.0):
    events = []
    source = LiveSource(path=tmp_path / "x.wav", speaker="Me")
    live = LiveTranscriber(
        DurationTranscriber(), [source], events.append, "s1", interval=interval, pressure=pressure
    )
    return live, events


def test_slow_ticks_stretch_the_interval_up_to_four_times(tmp_path):
    live, events = _adaptive(tmp_path)
    live._adapt(6.0)  # a 5 s tick took 6 s: falling behind
    assert live.interval == 10.0
    live._adapt(11.0)
    live._adapt(30.0)
    assert live.interval == 15.0  # capped at max_buffer / 2 (30 s buffer)
    assert [e["message"] for e in events] == [
        "Live · slowed down (transcription is slow)"
    ]  # one status per change, not per tick


def test_fast_ticks_shrink_the_interval_back(tmp_path):
    live, events = _adaptive(tmp_path)
    live._adapt(6.0)
    assert live.interval == 10.0
    for _ in range(4):
        live._adapt(1.0)
    assert live.interval == 10.0  # needs five easy ticks in a row
    live._adapt(1.0)
    assert live.interval == 5.0
    assert events[-1]["message"] == "Live"
    live._adapt(1.0)
    assert live.interval == 5.0  # never below the setting


def test_cpu_pressure_slows_down_even_when_ticks_are_fast(tmp_path):
    pressure = {"value": 75.0}
    live, events = _adaptive(tmp_path, pressure=lambda: pressure["value"])
    live._adapt(0.5)
    assert live.interval == 10.0
    assert events[-1]["message"] == "Live · slowed down (CPU busy)"
    pressure["value"] = 5.0
    for _ in range(5):
        live._adapt(0.5)
    assert live.interval == 5.0


def test_adaptive_off_keeps_the_interval(tmp_path):
    live, events = _adaptive(tmp_path)
    live.adaptive = False
    live._adapt(60.0)
    assert live.interval == 5.0 and events == []


def test_cpu_pressure_reads_psi(tmp_path):
    from mnemosyne.transcription.live import cpu_pressure

    psi = tmp_path / "cpu"
    psi.write_text(
        "some avg10=42.50 avg60=10.00 avg300=3.00 total=123\n"
        "full avg10=1.00 avg60=0.00 avg300=0.00 total=4\n"
    )
    assert cpu_pressure(psi) == 42.5
    assert cpu_pressure(tmp_path / "missing") is None
