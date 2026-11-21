"""Stand-ins for narration voice tests: a "voice" is a pitch, a recording is syllable-like tone
bursts at that pitch, and the speaker embedder tells pitches apart. Nothing is ever played."""

from __future__ import annotations

import io
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

RATE = 24000


def speech(freq: float, seconds: float, rate: int = RATE, level: float = 0.25) -> np.ndarray:
    """Tone bursts like syllables (0.2 s on, 0.12 s off) over a faint noise floor."""
    t = np.arange(int(seconds * rate)) / rate
    tone = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t)
    on = ((t % 0.32) < 0.2).astype(np.float64)
    rng = np.random.default_rng(int(freq * 10))
    noise = rng.normal(0, 10 ** (-65 / 20), t.size)
    return (level * tone * on / 1.3 + noise).astype(np.float32)


def wav_bytes(samples: np.ndarray, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32767
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    return data, rate


class PitchEmbedder:
    """A speaker embedder whose vector is a bump at the recording's main pitch."""

    space = "test-pitch"
    BINS = np.linspace(80, 420, 24)

    def __init__(self):
        self.loaded = False

    def is_loaded(self) -> bool:
        return self.loaded

    async def load(self) -> None:
        self.loaded = True

    async def unload(self) -> None:
        self.loaded = False

    async def embed(self, pcm: np.ndarray, sample_rate: int) -> list[float] | None:
        if pcm.size < sample_rate:
            return None
        spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
        freqs = np.fft.rfftfreq(pcm.size, 1 / sample_rate)
        band = (freqs > 60) & (freqs < 500)
        peak = float(freqs[band][np.argmax(spectrum[band])])
        return list(np.exp(-(((self.BINS - peak) / 15.0) ** 2)))


def _decode(path: Path, sample_rate: int = 48000) -> np.ndarray:
    data, rate = read_wav(Path(path))
    if rate == sample_rate:
        return data
    n = int(round(data.size * sample_rate / rate))
    return np.interp(np.linspace(0, data.size - 1, n), np.arange(data.size), data).astype(
        np.float32
    )


@pytest.fixture
def voice_models(ctx, monkeypatch, fake_live_transcriber):
    """The pitch embedder for voice checks, and WAV in place of ffmpeg where it is missing
    (CI has ffmpeg, so there the real decode and encode run)."""
    from mnemosyne.services import narration, voices
    from mnemosyne.services.model_service import ModelService

    embedder = PitchEmbedder()
    monkeypatch.setattr(ModelService, "live_embedder", property(lambda self: embedder))
    monkeypatch.setattr(voices, "can_check", lambda app: True)
    ctx.settings.synthesizer = "demo"
    narration.forget_engine(ctx)
    if shutil.which("ffmpeg") is None:
        from mnemosyne.audio import mixer
        from mnemosyne.synthesis.workers import write_wav

        monkeypatch.setattr(mixer, "decode_audio", _decode)

        def encode(samples, dest, title):
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_wav(dest, samples, narration.OUT_RATE)
            return dest

        monkeypatch.setattr(narration, "encode", encode)
    yield embedder
    narration.forget_engine(ctx)
