"""Recording lifecycle hardening: parts saved in order, failed saves not lost, statuses and
concurrency that stay right (api/routes/audio.py, services/recovery.py, jobs.py)."""

import asyncio
import shutil
import subprocess
import time
import wave
from pathlib import Path

import pytest

from mnemosyne.models.session import SessionStatus
from tests.conftest import stop_and_finish
from tests.test_parts import _wait, real_audio  # noqa: F401  (a fixture)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _slow_mix(monkeypatch, delay=0.4, fail_times=0):
    """Mixing that takes a while (and fails the first `fail_times` calls)."""
    from mnemosyne.api.routes import audio as audio_routes

    real = audio_routes.mix_audio_files
    calls = {"n": 0}

    def mix(inputs, output):
        calls["n"] += 1
        time.sleep(delay)
        if calls["n"] <= fail_times:
            raise RuntimeError("disk full")
        return real(inputs, output)

    monkeypatch.setattr(audio_routes, "mix_audio_files", mix)
    return calls


@needs_ffmpeg
def test_recording_again_while_the_last_part_is_saving(client, ctx, real_audio, monkeypatch):  # noqa: F811
    ctx.settings.auto_transcribe = False
    _slow_mix(monkeypatch)
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})  # saving in the background
    again = client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid})
    assert again.status_code == 200, again.text  # waited for the save, then numbered part 1
    assert ctx.active_recordings[sid].part == 1
    _, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert finish["status"] == "completed", finish["error"]
    parts = sorted(
        (r["part"], round(r["offset"], 1))
        for r in client.get(f"/api/sessions/{sid}").json()["recordings"]
    )
    assert parts == [(0, 0.0), (1, 5.0)]


@needs_ffmpeg
def test_a_failed_save_is_saved_before_the_next_part(client, ctx, real_audio, monkeypatch):  # noqa: F811
    ctx.settings.auto_transcribe = False
    _slow_mix(monkeypatch, delay=0, fail_times=1)
    from mnemosyne.services import recovery

    monkeypatch.setattr(recovery, "mix_audio_files", lambda inputs, out: _copy(inputs[0], out))
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    _, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert finish["status"] == "failed"  # disk full: the part waits for recovery
    folder = ctx.settings.recordings_dir / sid
    assert list(folder.glob("recording-*.json"))
    # Recording into it again saves that part first, then numbers the new one after it.
    assert (
        client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid}).status_code
        == 200
    )
    assert ctx.active_recordings[sid].part == 1
    _, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert finish["status"] == "completed", finish["error"]
    parts = sorted(r["part"] for r in client.get(f"/api/sessions/{sid}").json()["recordings"])
    assert parts == [0, 1] and not list(folder.glob("recording-*.json"))


@needs_ffmpeg
def test_a_part_that_cannot_be_saved_is_not_recorded_over(client, ctx, real_audio, monkeypatch):  # noqa: F811
    ctx.settings.auto_transcribe = False
    _slow_mix(monkeypatch, delay=0, fail_times=99)
    from mnemosyne.services import recovery

    def broken(inputs, out):
        raise RuntimeError("still full")

    monkeypatch.setattr(recovery, "mix_audio_files", broken)
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    stop_and_finish(client, sid, {"transcribe": False})
    again = client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid})
    assert again.status_code == 409 and "new meeting" in again.json()["detail"]
    assert sid not in ctx.active_recordings


def _copy(src, out):
    out = Path(out).with_suffix(".ogg")
    shutil.copy(src, out)
    return out


def test_cancelling_a_waiting_job_keeps_the_limit(client, ctx):
    running = []

    def runner(tag):
        async def run(job):
            running.append(tag)
            assert len([t for t in running if t != "done"]) <= 1, "two at once"
            await asyncio.sleep(0.2)
            running.remove(tag)
            return {}

        return run

    async def go():
        jobs = [ctx.jobs.submit("transcribe", runner(i)) for i in range(3)]
        await asyncio.sleep(0.05)
        await ctx.jobs.cancel(jobs[1].id)  # waiting for its turn
        extra = ctx.jobs.submit("transcribe", runner("x"))
        for j in (jobs[0], jobs[2], extra):
            done = await ctx.jobs.wait(j.id)
            assert done.status == "completed", done.error

    client.portal.call(go)


def test_a_busy_meeting_is_not_deleted(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 409
    stop_and_finish(client, sid, {"transcribe": False})
    assert client.delete(f"/api/sessions/{sid}").status_code == 200


def test_a_transcription_left_unfinished_is_reset_at_start(settings, keystore):
    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app

    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    sid = ctx.sessions.create_session("m").id
    ctx.sessions.set_status(sid, SessionStatus.TRANSCRIBING)  # and then the backend died
    with TestClient(create_app(settings, keystore=keystore)) as client:
        assert client.get(f"/api/sessions/{sid}").json()["status"] == "created"


def test_a_recording_meeting_keeps_its_status(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    ctx.sessions.set_status(sid, SessionStatus.COMPLETED)  # e.g. a transcription of part 0 ends
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "recording"
    stop_and_finish(client, sid, {"transcribe": False})


@needs_ffmpeg
def test_a_silent_device_does_not_shift_the_others(tmp_path):
    from mnemosyne.audio.capture import RecordingProcess, RecordingSession, stop_recording

    class Done:
        returncode = 0

    empty = tmp_path / "a.wav"
    empty.write_bytes(b"")
    good = tmp_path / "b.wav"
    with wave.open(str(good), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x10" * 16000)
    rec = RecordingSession(session_id="r", output_dir=tmp_path)
    rec.processes = [
        RecordingProcess(device_id=1, process=Done(), output_path=empty),
        RecordingProcess(device_id=2, process=Done(), output_path=good),
    ]
    files = asyncio.run(stop_recording(rec))
    assert files[0] is None and files[1].name == "b.ogg"
    assert subprocess.run(["ffprobe", "-v", "error", str(files[1])]).returncode == 0
