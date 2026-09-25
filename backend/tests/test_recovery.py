"""Recovering recordings interrupted by a crash (services/recovery.py)."""

import json
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemosyne.models.session import SessionStatus
from mnemosyne.services import recovery

RATE = 16000


def write_wav(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x01\x00" * int(RATE * seconds))
    return path


def break_header(path: Path, value: int = 0) -> None:
    """What pw-record leaves behind when it is killed: sizes never filled in."""
    with path.open("r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", value))
        f.seek(40)
        f.write(struct.pack("<I", value))


@pytest.mark.parametrize("value", [0, 0xFFFFFFFF])
def test_repair_wav_fixes_sizes(tmp_path, value):
    path = write_wav(tmp_path / "a.wav", 1.5)
    break_header(path, value)
    assert recovery.repair_wav(path) == pytest.approx(1.5)
    with wave.open(str(path)) as w:
        assert w.getnframes() == int(RATE * 1.5)


def test_repair_wav_leaves_good_files_and_rejects_others(tmp_path):
    good = write_wav(tmp_path / "good.wav", 0.5)
    before = good.read_bytes()
    assert recovery.repair_wav(good) == pytest.approx(0.5)
    assert good.read_bytes() == before
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav at all")
    assert recovery.repair_wav(junk) == 0.0
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    assert recovery.repair_wav(empty) == 0.0


def test_stop_orphan_recorders_only_stops_ours(tmp_path):
    ours = tmp_path / "ours.wav"
    other = tmp_path / "other.wav"
    ours.write_bytes(b"")
    other.write_bytes(b"")
    # Stand-ins named pw-record (tail -f runs until signalled).
    mine = subprocess.Popen(["bash", "-c", f"exec -a pw-record tail -f {ours}"])
    theirs = subprocess.Popen(["bash", "-c", f"exec -a pw-record tail -f {other}"])
    try:
        time.sleep(0.2)
        assert recovery.stop_orphan_recorders([ours]) == 1
        assert mine.wait(timeout=5) is not None
        assert theirs.poll() is None
    finally:
        for p in (mine, theirs):
            p.kill()
            p.wait()


@pytest.fixture
def fake_encoding(monkeypatch):
    """No ffmpeg: 'encode' by renaming, 'mix' by writing a marker file."""

    async def convert_to_opus(wav: Path) -> Path:
        ogg = wav.with_suffix(".ogg")
        wav.rename(ogg)
        return ogg

    def mix_audio_files(inputs, output):
        output = Path(output).with_suffix(".ogg")
        output.write_bytes(b"mixed:" + b",".join(Path(i).name.encode() for i in inputs))
        return output

    monkeypatch.setattr(recovery, "convert_to_opus", convert_to_opus)
    monkeypatch.setattr(recovery, "mix_audio_files", mix_audio_files)


def interrupted(ctx, status=SessionStatus.RECORDING, name="Standup") -> tuple[str, Path]:
    ctx.settings.auto_transcribe = False
    sid = ctx.sessions.create_session(name).id
    ctx.sessions.set_status(sid, status)
    return sid, ctx.settings.recordings_dir / sid


def manifest(folder: Path, tracks: list[tuple[str, str]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / recovery.MANIFEST).write_text(
        json.dumps(
            {
                "recording_id": "rec1",
                "tracks": [
                    {"device_id": i, "device_name": f"Dev {i}", "source": src, "wav": wav}
                    for i, (wav, src) in enumerate(tracks)
                ],
            }
        )
    )


def wait_for(ctx, kind: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [j for j in ctx.jobs.jobs.values() if j.kind == kind]
        if jobs and all(j.is_terminal for j in jobs):
            return jobs
        time.sleep(0.05)
    raise AssertionError(f"{kind} jobs did not finish")


def test_startup_recovers_an_interrupted_recording(app, ctx, fake_encoding):
    sid, folder = interrupted(ctx)
    manifest(folder, [("rec1_device_1.wav", "mic"), ("rec1_device_2.wav", "system")])
    break_header(write_wav(folder / "rec1_device_1.wav", 2.0))
    write_wav(folder / "rec1_device_2.wav", 1.0)

    with TestClient(app) as client:
        (job,) = wait_for(ctx, "recover")
        assert job.status == "completed", job.error
        session = client.get(f"/api/sessions/{sid}").json()
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()

    assert session["status"] == "created"
    assert session["audio_file"].endswith("rec1_mixed.ogg")
    assert sorted(r["source"] for r in session["recordings"]) == ["mic", "system"]
    assert not list(folder.glob("*.wav"))
    (done,) = hello["recovered"]
    assert done["session_id"] == sid
    assert done["name"] == "Standup"
    assert done["seconds"] == pytest.approx(2.0)
    assert done["transcribing"] is False


def test_recovery_without_manifest_uses_every_wav(app, ctx, fake_encoding):
    sid, folder = interrupted(ctx, SessionStatus.ENCODING)
    write_wav(folder / "x_device_7.wav")
    with TestClient(app):
        (job,) = wait_for(ctx, "recover")
        session = ctx.sessions.get_session(sid)
    assert job.status == "completed", job.error
    assert [r.device_name for r in session.recordings] == ["Recovered track 1"]


def test_recovery_without_audio_marks_the_session_failed(app, ctx, fake_encoding):
    sid, folder = interrupted(ctx)
    manifest(folder, [("rec1_device_1.wav", "mic")])
    (folder / "rec1_device_1.wav").write_bytes(b"RIFF\0\0\0\0WAVE")  # header only
    with TestClient(app):
        (job,) = wait_for(ctx, "recover")
        status = ctx.sessions.get_session(sid).status
    assert job.status == "failed"
    assert status == SessionStatus.ERROR
    assert ctx.recovered == []


def test_recovery_transcribes_when_auto_transcribe_is_on(app, ctx, fake_encoding, fake_engine):
    sid, folder = interrupted(ctx)
    ctx.settings.auto_transcribe = True
    manifest(folder, [("rec1_device_1.wav", "mic")])
    write_wav(folder / "rec1_device_1.wav")
    with TestClient(app):
        wait_for(ctx, "recover")
        (job,) = wait_for(ctx, "transcribe")
    assert job.session_id == sid
    assert ctx.recovered[0].transcribing is True


def test_active_and_finished_sessions_are_left_alone(ctx):
    live, _ = interrupted(ctx)
    ctx.active_recordings[live] = object()
    done = ctx.sessions.create_session("Done").id
    stuck, _ = interrupted(ctx)
    assert recovery.interrupted_sessions(ctx) == [stuck]
    assert done not in recovery.interrupted_sessions(ctx)


def test_recording_start_writes_the_manifest(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()["session_id"]
    data = json.loads((ctx.settings.recordings_dir / sid / recovery.MANIFEST).read_text())
    assert data["recording_id"] == "rec00001"
    assert [(t["wav"], t["source"]) for t in data["tracks"]] == [
        ("dev1.wav", "mic"),
        ("dev2.wav", "system"),
    ]
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
