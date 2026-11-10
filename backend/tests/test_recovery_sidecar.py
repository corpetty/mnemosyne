"""Recovery with the capture sidecar (capture/) as the recorder instead of pw-record: the real
debug binary with `--fake tone` (no audio hardware). Skipped when it is not built; see
test_capture_sidecar.py."""

import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemosyne.models.session import SessionStatus
from mnemosyne.services import recovery
from tests.test_capture_sidecar import BIN
from tests.test_recovery import wait_for

pytestmark = pytest.mark.skipif(BIN is None, reason="the capture sidecar is not built")

WAV = "rec1_device_1.wav"


@pytest.fixture
def fake_encoding(monkeypatch):
    async def convert_to_opus(wav: Path) -> Path:
        ogg = wav.with_suffix(".ogg")
        wav.rename(ogg)
        return ogg

    def mix_audio_files(inputs, output):
        output = Path(output).with_suffix(".ogg")
        output.write_bytes(b"mixed")
        return output

    monkeypatch.setattr(recovery, "convert_to_opus", convert_to_opus)
    monkeypatch.setattr(recovery, "mix_audio_files", mix_audio_files)


def interrupted_recording(ctx) -> tuple[str, Path]:
    ctx.settings.auto_transcribe = False
    sid = ctx.sessions.create_session("Standup").id
    ctx.sessions.set_status(sid, SessionStatus.RECORDING)
    folder = ctx.settings.recordings_dir / sid
    folder.mkdir(parents=True)
    (folder / recovery.MANIFEST).write_text(
        json.dumps(
            {
                "recording_id": "rec1",
                "tracks": [{"device_id": 1, "device_name": "Tone", "source": "mic", "wav": WAV}],
            }
        )
    )
    return sid, folder


def orphan_sidecar(out: Path) -> int:
    """Start the sidecar the way a backend would, then let its parent go: a shell starts it in
    the background and exits, so init (or a subreaper) adopts it."""
    script = (
        '"$0" record --fake tone --size-interval-ms 200 "$1" </dev/null >/dev/null 2>&1 & echo $!'
    )
    result = subprocess.run(
        ["sh", "-c", script, str(BIN), str(out)], capture_output=True, text=True, timeout=10
    )
    return int(result.stdout.strip())


def wait_size(path: Path, size: int) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() or path.stat().st_size < size:
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_an_orphan_sidecar_is_found_stopped_and_its_recording_saved(app, ctx, fake_encoding):
    sid, folder = interrupted_recording(ctx)
    out = folder / WAV
    pid = orphan_sidecar(out)
    try:
        wait_size(out, 44 + 2 * 48000)
        assert recovery._recorders_writing({str(out)}) == [pid]
        assert recovery.recorder_owner([out]) is None  # nobody's child but init's: ours to take

        with TestClient(app):
            (job,) = wait_for(ctx, "recover")
            session = ctx.sessions.get_session(sid)
        assert job.status == "completed", job.error
        assert session.status == SessionStatus.CREATED
        assert [(r.source, r.device_name) for r in session.recordings] == [("mic", "Tone")]
        assert session.audio_file.endswith("rec1_mixed.ogg")
        # SIGTERM, not SIGKILL: the sidecar finalized the WAV before the fake encoder moved it.
        saved = Path(session.recordings[0].path)
        with wave.open(str(saved)) as w:
            assert w.getframerate() == 48000
            assert w.getnframes() == (saved.stat().st_size - 44) // 2
            assert w.getnframes() >= 48000
        assert ctx.recovered[0].seconds >= 1.0
        assert not (folder / recovery.MANIFEST).exists()
        assert recovery._recorders_writing({str(out)}) == []
    finally:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True)


def test_a_sigkilled_sidecar_recording_is_repaired_and_saved(app, ctx, fake_encoding):
    sid, folder = interrupted_recording(ctx)
    out = folder / WAV
    proc = subprocess.Popen(
        [str(BIN), "record", "--fake", "tone", str(out)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_size(out, 44 + 2 * 48000)
        proc.kill()  # default 5 s size interval: the header still has the placeholder sizes
    finally:
        proc.wait(timeout=10)
    with TestClient(app):
        (job,) = wait_for(ctx, "recover")
        session = ctx.sessions.get_session(sid)
    assert job.status == "completed", job.error
    assert session.status == SessionStatus.CREATED
    saved = Path(session.recordings[0].path)
    with wave.open(str(saved)) as w:
        assert w.getnframes() == (saved.stat().st_size - 44) // 2 >= 48000


def test_a_sidecar_with_a_live_python_parent_is_left_alone(app, ctx, fake_encoding):
    sid, folder = interrupted_recording(ctx)
    out = folder / WAV
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.argv[1], 'record', '--fake', 'tone', sys.argv[2]],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    backend = subprocess.Popen(
        [sys.executable, "-c", code, str(BIN), str(out)], stdout=subprocess.PIPE, text=True
    )
    try:
        pid = int(backend.stdout.readline())
        wait_size(out, 44 + 2 * 4800)
        assert recovery.recorder_owner([out]) == backend.pid
        with TestClient(app):
            time.sleep(0.5)
            assert not [j for j in ctx.jobs.jobs.values() if j.kind == "recover"]
            assert ctx.sessions.get_session(sid).status == SessionStatus.RECORDING
        import psutil

        assert psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    finally:
        backend.kill()
        backend.wait()
        subprocess.run(["pkill", "-9", "-f", "mnemosyne-captur[e] record"], capture_output=True)
