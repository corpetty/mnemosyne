"""Mix multiple audio files into a single Opus output."""

import re
import subprocess
from pathlib import Path

import numpy as np

# ffmpeg work on a long meeting (hours) is minutes, never this much: a hung ffmpeg still ends.
LONG_TIMEOUT = 4 * 3600
PEAK = 0.95  # mixes are normalized to this peak


def decode_audio(path: Path, sample_rate: int = 48000) -> np.ndarray:
    """Decode any audio file to raw PCM float32 mono using ffmpeg."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(path),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-",
        ],
        capture_output=True,
        timeout=LONG_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {result.stderr.decode()[-500:]}")
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
            "ffmpeg",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "-",
            "-c:a",
            "libopus",
            "-b:a",
            bitrate,
            str(output_path),
        ],
        input=pcm_bytes,
        capture_output=True,
        timeout=LONG_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {result.stderr.decode()[-500:]}")
    return output_path


def normalize_audio(audio: np.ndarray) -> np.ndarray:
    """Normalize audio to [-0.95, 0.95] range."""
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * PEAK
    return audio


def _graph(n: int) -> str:
    """The mean of the inputs (shorter ones end in silence), mono, 48 kHz."""
    if n == 1:
        return "[0:a]aformat=channel_layouts=mono:sample_rates=48000[mix]"
    inputs = "".join(f"[{i}:a]" for i in range(n))
    return (
        f"{inputs}amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0,"
        f"volume={1 / n:.6f},aformat=channel_layouts=mono:sample_rates=48000[mix]"
    )


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        timeout=LONG_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mix failed: {result.stderr[-500:]}")
    return result


def mix_audio_files(input_paths: list[Path], output_path: Path) -> Path:
    """Mix audio files (any format ffmpeg reads) into one mono OGG/Opus file, peak-normalized.

    ffmpeg streams it in two passes (measure the peak, then encode with the gain), so memory
    stays flat however long the meeting: decoding hours of audio into memory needed tens of GB.
    """
    output_path = output_path.with_suffix(".ogg")
    inputs = [arg for p in input_paths for arg in ("-i", str(p))]
    graph = _graph(len(input_paths))
    probe = _ffmpeg([*inputs, "-filter_complex", f"{graph};[mix]volumedetect", "-f", "null", "-"])
    peaks = re.findall(r"max_volume:\s*(-?[\d.]+|-inf) dB", probe.stderr)
    gain_db = 0.0
    if peaks and peaks[-1] != "-inf":
        gain_db = 20 * np.log10(PEAK) - float(peaks[-1])  # to the old normalized peak
    tmp = output_path.with_name(f".{output_path.name}.mixing.ogg")
    _ffmpeg(
        [
            "-y",
            *inputs,
            "-filter_complex",
            f"{graph};[mix]volume={gain_db:.3f}dB[out]",
            "-map",
            "[out]",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            str(tmp),
        ]
    )
    tmp.replace(output_path)
    return output_path
