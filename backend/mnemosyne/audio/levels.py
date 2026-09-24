"""Audio levels for meters, and the system-audio capture self-test."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path

import numpy as np

from ..models.base import ApiModel
from .capture import AudioDevice, build_record_command

logger = logging.getLogger(__name__)

FLOOR_DB = -90.0


class Level(ApiModel):
    rms_db: float
    peak_db: float


def level_of(pcm: np.ndarray) -> Level:
    """RMS and peak in dBFS for int16 or float PCM."""
    if pcm.size == 0:
        return Level(rms_db=FLOOR_DB, peak_db=FLOOR_DB)
    x = pcm.astype(np.float64)
    if pcm.dtype == np.int16:
        x /= 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))

    def db(v: float) -> float:
        return round(max(20 * math.log10(v), FLOOR_DB), 1) if v > 0 else FLOOR_DB

    return Level(rms_db=db(rms), peak_db=db(peak))


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return pcm, rate


async def _record_for(device: AudioDevice, path: Path, seconds: float, node_name: str):
    cmd = build_record_command(device, path, 48000, 1, "s16", node_name=node_name)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await asyncio.sleep(seconds)
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def sample_level(device: AudioDevice, seconds: float = 1.0) -> Level:
    """Record `seconds` from a device and return its level."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "level.wav"
        await _record_for(device, path, seconds, f"mnemosyne-level-{uuid.uuid4().hex[:6]}")
        if not path.exists():
            return Level(rms_db=FLOOR_DB, peak_db=FLOOR_DB)
        pcm, _ = read_wav(path)
        return level_of(pcm)


def linked_sources(node_name: str) -> list[str]:
    """Ports feeding the given node, from `pw-link -l` (e.g. "alsa_output...:monitor_FL")."""
    try:
        out = subprocess.run(["pw-link", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    sources, current = [], None
    for line in out.splitlines():
        if not line.startswith((" ", "\t")):
            current = line.strip()
            continue
        m = re.match(r"\s*\|<-\s*(.+)$", line)
        if m and current and current.split(":", 1)[0] == node_name:
            sources.append(m.group(1).strip())
    return sources


def tone_level(pcm: np.ndarray, rate: int, freq: float = 1000.0) -> float:
    """Strongest level (dB relative to the median spectrum) of `freq` in any 100 ms window."""
    win = rate // 10
    best = 0.0
    freqs = np.fft.rfftfreq(win, 1 / rate)
    band = (freqs > freq - 20) & (freqs < freq + 20)
    x = pcm.astype(np.float64)
    for i in range(len(x) // win):
        spec = np.abs(np.fft.rfft(x[i * win : (i + 1) * win] * np.hanning(win)))
        ratio = spec[band].max() / (np.median(spec) + 1e-9)
        best = max(best, float(ratio))
    # Digital silence makes the ratio explode; cap it so the number stays meaningful.
    return round(min(20 * math.log10(best), 60.0), 1) if best > 0 else 0.0


class SelfTestResult(ApiModel):
    passed: bool
    tone_detected: bool
    tone_snr_db: float
    level: Level
    linked_from: list[str]
    captures_monitor: bool
    message: str


def _tone_wav(path: Path, rate: int = 48000, seconds: float = 0.6, amplitude: float = 0.1):
    t = np.arange(int(rate * seconds)) / rate
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.02)  # 20 ms fades, no clicks
    pcm = (amplitude * np.sin(2 * np.pi * 1000 * t) * fade * 32767).astype(np.int16)
    silence = np.zeros(int(rate * 0.2), dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.concatenate([silence, pcm, silence]).tobytes())


async def self_test(sink: AudioDevice) -> SelfTestResult:
    """Record `sink` exactly as a recording would, play a quiet tone through it, and
    report whether the tone was captured and where the recorder was connected."""
    node = f"mnemosyne-selftest-{uuid.uuid4().hex[:6]}"
    with tempfile.TemporaryDirectory() as tmp:
        rec_path, tone_path = Path(tmp) / "rec.wav", Path(tmp) / "tone.wav"
        _tone_wav(tone_path)
        cmd = build_record_command(sink, rec_path, 48000, 1, "s16", node_name=node)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            await asyncio.sleep(0.5)
            links = linked_sources(node)
            play = await asyncio.create_subprocess_exec(
                "pw-play",
                "--target",
                sink.name,
                str(tone_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(play.wait(), timeout=10)
            await asyncio.sleep(0.4)
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        pcm, rate = read_wav(rec_path) if rec_path.exists() else (np.zeros(0, np.int16), 48000)

    snr = tone_level(pcm, rate) if pcm.size else 0.0
    detected = snr >= 20.0
    monitor = bool(links) and all(
        link.split(":", 1)[0] == sink.name and ":monitor_" in link for link in links
    )
    if not links:
        msg = "The recorder did not connect to anything. Is PipeWire running?"
    elif not monitor:
        msg = (
            "System audio is NOT being captured from this output: the recorder connected to "
            + ", ".join(sorted({x.split(":", 1)[0] for x in links}))
            + " instead."
        )
    elif not detected:
        msg = (
            "Connected to the output's monitor, but the test tone was not heard. The output may "
            "be muted, or audio may be routed to a different device."
        )
    else:
        msg = "System audio capture works: the test tone was recorded from this output."
    return SelfTestResult(
        passed=monitor and detected,
        tone_detected=detected,
        tone_snr_db=snr,
        level=level_of(pcm),
        linked_from=links,
        captures_monitor=monitor,
        message=msg,
    )
