"""The capture sidecar (capture/, a Rust binary) keeps pw-record's contract with the backend:
the same placeholder WAV header, a file the live transcript can tail while it grows, a file
recovery can repair after a hard kill. These tests drive the real binary with its fake sources
(no audio hardware) and skip when it is not built: set MNEMOSYNE_CAPTURE_BIN, or build it with
`cargo build --manifest-path capture/Cargo.toml` (CI does)."""

import json
import os
import signal
import subprocess
import time
import wave
from pathlib import Path

import pytest

from mnemosyne.audio.capture import wav_header
from mnemosyne.services.recovery import repair_wav
from mnemosyne.transcription.live import WavTail


def _binary() -> Path | None:
    env = os.environ.get("MNEMOSYNE_CAPTURE_BIN")
    if env:
        return Path(env) if Path(env).is_file() else None
    root = Path(__file__).resolve().parents[2] / "capture" / "target"
    for kind in ("release", "debug"):
        for name in ("mnemosyne-capture", "mnemosyne-capture.exe"):
            if (root / kind / name).is_file():
                return root / kind / name
    return None


BIN = _binary()
pytestmark = pytest.mark.skipif(BIN is None, reason="the capture sidecar is not built")


def _start(out: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [str(BIN), "record", "--fake", "tone", *args, str(out)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_for_size(path: Path, size: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() or path.stat().st_size < size:
        assert time.monotonic() < deadline, f"{path} never reached {size} bytes"
        time.sleep(0.02)


def test_header_is_the_placeholder_pw_record_gets_and_the_file_can_be_tailed(tmp_path):
    out = tmp_path / "a.wav"
    proc = _start(out)
    try:
        _wait_for_size(out, 44 + 2 * 4800)
        assert out.read_bytes()[:44] == wav_header(48000, 1)
        tail = WavTail(out)
        samples = tail.read_new()
        assert (tail.sample_rate, tail.channels, tail.sample_width) == (48000, 1, 2)
        assert len(samples) >= 4800
        assert int(abs(samples).max()) > 1000  # the tone is there
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert proc.returncode == 0


def test_sigterm_leaves_final_sizes(tmp_path):
    out = tmp_path / "a.wav"
    proc = _start(out)
    _wait_for_size(out, 44 + 2 * 9600)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 0
    data = out.read_bytes()
    seconds = repair_wav(out, fix=False)
    assert seconds >= 0.2
    with wave.open(str(out)) as w:  # opens: the sizes are real, not the placeholder
        assert w.getframerate() == 48000
        assert w.getnframes() == (len(data) - 44) // 2
    assert out.read_bytes() == data  # measuring changed nothing: nothing left to repair


def test_a_hard_killed_recording_is_repaired_by_recovery(tmp_path):
    out = tmp_path / "a.wav"
    proc = _start(out, "--size-interval-ms", "200")
    try:
        time.sleep(1.5)
        proc.kill()  # no chance to fix anything
    finally:
        proc.wait(timeout=10)
    size_before = out.stat().st_size
    with wave.open(str(out)) as w:  # already readable: the sizes were rewritten while recording
        assert w.getnframes() > 0
    seconds = repair_wav(out)
    assert seconds >= 0.5
    assert out.stat().st_size == size_before
    with wave.open(str(out)) as w:
        assert w.getnframes() == (size_before - 44) // 2
        assert abs(w.getnframes() / 48000 - seconds) < 1e-6


def test_devices_json_is_a_list():
    out = subprocess.run([str(BIN), "devices"], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0
    assert isinstance(json.loads(out.stdout), list)
