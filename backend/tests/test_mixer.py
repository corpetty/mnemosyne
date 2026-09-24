"""Audio mixing. Uses real ffmpeg when available; skipped otherwise."""

import shutil
import wave

import numpy as np
import pytest

from mnemosyne.audio.mixer import decode_audio, mix_audio_files, normalize_audio

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

SR = 48000


def _write_wav(path, seconds: float, freq: float, amp: float = 0.5):
    t = np.arange(int(SR * seconds)) / SR
    pcm = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def test_normalize_scales_peak():
    audio = np.array([0.1, -0.5, 0.25], dtype=np.float32)
    out = normalize_audio(audio)
    assert np.isclose(np.abs(out).max(), 0.95)


def test_normalize_silence_is_noop():
    audio = np.zeros(10, dtype=np.float32)
    assert np.array_equal(normalize_audio(audio), audio)


@needs_ffmpeg
def test_mix_pads_to_longest_and_encodes_opus(tmp_path):
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _write_wav(a, seconds=1.0, freq=440)
    _write_wav(b, seconds=2.0, freq=880)

    out = mix_audio_files([a, b], tmp_path / "mixed.wav")
    assert out.suffix == ".ogg"
    assert out.exists()

    mixed = decode_audio(out)
    # Opus adds a little pre-skip/padding; allow 100 ms slack around 2 s.
    assert abs(len(mixed) / SR - 2.0) < 0.1
    assert np.abs(mixed).max() > 0.5


@needs_ffmpeg
def test_single_input_is_normalized_and_encoded(tmp_path):
    a = tmp_path / "a.wav"
    _write_wav(a, seconds=0.5, freq=440, amp=0.1)
    out = mix_audio_files([a], tmp_path / "single.wav")
    mixed = decode_audio(out)
    assert np.abs(mixed).max() > 0.8  # normalized up from 0.1
