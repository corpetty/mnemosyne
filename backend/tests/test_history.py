"""A meeting's history: its parts, gaps, files, audio waiting to be saved, and the log
(services/history.py, routes/history.py)."""

import json
import os
import shutil
import wave
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mnemosyne.models.session import Recording
from tests.conftest import stop_and_finish
from tests.test_parts import _audio, _wait, real_audio  # noqa: F401  (a fixture)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _history(client, sid) -> dict:
    r = client.get(f"/api/sessions/{sid}/history")
    assert r.status_code == 200, r.text
    return r.json()


def _kinds(h) -> list[str]:
    return [e["kind"] for e in h["events"]]


@needs_ffmpeg
def test_parts_gaps_and_log_of_a_meeting_recorded_twice(client, ctx, real_audio):  # noqa: F811
    ctx.settings.auto_transcribe = False
    sid = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()["session_id"]
    stop_and_finish(client, sid, {"transcribe": False})
    client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid})
    stop_and_finish(client, sid, {"transcribe": False})

    h = _history(client, sid)
    assert [(p["part"], p["how"], round(p["seconds"])) for p in h["parts"]] == [
        (0, "recorded", 5),
        (1, "recorded", 2),
    ]
    assert h["parts"][1]["offset"] == pytest.approx(5.0, abs=0.1)
    assert not h["parts"][0]["approximate"] and h["parts"][1]["gap_before"] is not None
    assert [f["source"] for f in h["parts"][0]["files"]] == ["mic", "system"]
    assert all(f["size"] for p in h["parts"] for f in p["files"])
    assert h["recorded_seconds"] == pytest.approx(7.0, abs=0.2)
    assert h["pending"] == [] and h["missing"] == [] and h["orphans"] == []
    assert _kinds(h) == [
        "created",
        "recording_started",
        "recording_stopped",
        "part_saved",
        "recording_started",
        "recording_stopped",
        "part_saved",
    ]
    stopped = [e for e in h["events"] if e["kind"] == "recording_stopped"]
    assert [e["detail"]["reason"] for e in stopped] == ["stop", "stop"]


@needs_ffmpeg
def test_the_recording_in_progress_is_a_part_not_lost_audio(client, ctx, real_audio):  # noqa: F811
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    h = _history(client, sid)
    assert h["recording_now"]
    assert [p["how"] for p in h["parts"]] == ["recording"]
    assert h["pending"] == []  # its manifest names WAVs still being written
    stop_and_finish(client, sid, {"transcribe": False})


def _interrupted(folder: Path, rec: str, part: int, seconds: float = 1.0) -> None:
    """What a backend killed mid-recording leaves: a manifest and a WAV."""
    folder.mkdir(parents=True, exist_ok=True)
    with wave.open(str(folder / f"{rec}_device_1.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * int(16000 * seconds))
    manifest = {
        "recording_id": rec,
        "part": part,
        "tracks": [
            {"device_id": 1, "device_name": "Mic", "source": "mic", "wav": f"{rec}_device_1.wav"}
        ],
    }
    (folder / f"recording-{rec}.json").write_text(json.dumps(manifest))


@needs_ffmpeg
def test_audio_waiting_to_be_saved_is_shown_and_recovered(client, ctx):
    ctx.settings.auto_transcribe = False
    sid = client.post("/api/sessions", json={"name": "lost and found"}).json()["id"]
    folder = ctx.settings.recordings_dir / sid
    _interrupted(folder, "lost0001", 0, seconds=1.5)
    stamp = datetime(2026, 9, 1, 10, 0, 0).timestamp()
    os.utime(folder / "lost0001_device_1.wav", (stamp, stamp))

    h = _history(client, sid)
    assert h["parts"] == []
    [waiting] = h["pending"]
    assert waiting["part"] == 0 and waiting["state"] == "waiting"
    assert waiting["seconds"] == pytest.approx(1.5, abs=0.01)
    assert [f["name"] for f in waiting["files"]] == ["lost0001_device_1.wav"]

    job = client.post(f"/api/sessions/{sid}/recover").json()
    assert _wait(client, job["id"])["status"] == "completed"
    h = _history(client, sid)
    assert h["pending"] == []
    assert [(p["part"], p["how"]) for p in h["parts"]] == [(0, "recovered")]
    assert h["parts"][0]["seconds"] == pytest.approx(1.5, abs=0.1)
    assert "recovered" in _kinds(h)
    # Shown when it was recorded (its WAV's last write), not when it was recovered.
    assert h["parts"][0]["ended_at"].startswith("2026-09-01T10:00:00")
    assert not h["parts"][0]["approximate"]
    assert client.post(f"/api/sessions/{sid}/recover").status_code == 404  # nothing left


@needs_ffmpeg
def test_missing_and_stray_files(client, ctx, real_audio):  # noqa: F811
    ctx.settings.auto_transcribe = False
    sid = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()["session_id"]
    stop_and_finish(client, sid, {"transcribe": False})
    session = ctx.sessions.get_session(sid)
    gone = Path(session.recordings[0].path)
    gone.unlink()
    folder = ctx.settings.recordings_dir / sid
    _audio(folder / "stray_mixed.ogg", 1.0)
    (folder / ".joining-3.ogg").write_bytes(b"half")  # being written: not a stray

    h = _history(client, sid)
    assert h["missing"] == [gone.name]
    assert [f["name"] for f in h["orphans"]] == ["stray_mixed.ogg"]
    assert h["parts"][0]["files"][0]["size"] is None


@needs_ffmpeg
def test_a_meeting_from_before_the_log_is_worked_out_from_its_recordings(client, ctx, tmp_path):
    sid = client.post("/api/sessions", json={"name": "older"}).json()["id"]
    folder = ctx.settings.recordings_dir / sid
    folder.mkdir(parents=True)
    a, b = _audio(folder / "a.ogg", 3.0), _audio(folder / "b.ogg", 2.0)
    mixed = _audio(folder / "meeting_1.ogg", 5.0)
    recs = [
        Recording(source="mic", device_id=1, device_name="Mic", path=str(a), part=0, offset=0.0),
        Recording(source="mic", device_id=1, device_name="Mic", path=str(b), part=1, offset=3.0),
    ]
    ctx.sessions.set_audio(sid, str(mixed), recs)

    h = _history(client, sid)
    assert [(p["part"], round(p["seconds"], 1), p["approximate"]) for p in h["parts"]] == [
        (0, 3.0, True),
        (1, 2.0, True),
    ]
    assert _kinds(h) == ["created"]


def test_shutdown_while_recording_is_in_the_log(app, settings, fake_pipewire):
    from mnemosyne.storage.sqlite import SessionRepository

    with TestClient(app) as c:
        sid = c.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    events = SessionRepository(settings.db_path).events(sid)  # the app's is closed now
    assert events[-1]["kind"] == "recording_stopped"
    assert events[-1]["detail"]["reason"] == "shutdown"


def test_unknown_meeting(client):
    assert client.get("/api/sessions/nope/history").status_code == 404
    assert client.post("/api/sessions/nope/recover").status_code == 404
