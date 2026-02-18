"""Mix multiple audio files into a single Opus output."""

import subprocess
from pathlib import Path

import numpy as np


def decode_audio(path: Path, sample_rate: int = 48000) -> np.ndarray:
    """Decode any audio file to raw PCM float32 mono using ffmpeg."""
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(path),
            "-f", "f32le", "-acodec", "pcm_f32le",
            "-ac", "1", "-ar", str(sample_rate),
            "-"
        ],
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {result.stderr.decode()}")
    return np.frombuffer(result.stdout, dtype=np.float32)


def encode_opus(
    audio: np.ndarray,
    output_path: Path,
    sample_rate: int = 48000,
    bitrate: str = "64k",
) -> Path:
    """Encode float32 mono PCM to OGG/Opus via ffmpeg."""
    pcm_bytes = (audio * 32767).astype(np.int16).tobytes()
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "-",
            "-c:a", "libopus", "-b:a", bitrate,
            str(output_path),
        ],
        input=pcm_bytes,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {result.stderr.decode()}")
    return output_path


def normalize_audio(audio: np.ndarray) -> np.ndarray:
    """Normalize audio to [-0.95, 0.95] range."""
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * 0.95
    return audio


def mix_audio_files(input_paths: list[Path], output_path: Path) -> Path:
    """Mix multiple audio files into a single mono OGG/Opus file.

    Accepts any format ffmpeg can decode (WAV, OGG, FLAC, etc.).
    """
    output_path = output_path.with_suffix(".ogg")

    if len(input_paths) == 1:
        data = decode_audio(input_paths[0])
        data = normalize_audio(data)
        return encode_opus(data, output_path)

    signals = [decode_audio(path) for path in input_paths]

    # Pad shorter signals to match the longest
    max_len = max(len(s) for s in signals)
    padded = [np.pad(s, (0, max_len - len(s))) for s in signals]

    mixed = np.mean(padded, axis=0).astype(np.float32)
    mixed = normalize_audio(mixed)

    return encode_opus(mixed, output_path)
