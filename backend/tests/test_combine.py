"""Combining meetings, and adding an audio file to one (services/combine.py, import as a part)."""

import io
import shutil
import subprocess
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mnemosyne.models.session import Recording
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.parts import audio_seconds

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _audio(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=300:d={seconds}",
         "-c:a", "libopus", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _wait(client, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while (job := client.get(f"/api/jobs/{job_id}").json())["status"] not in (
        "completed",
        "failed",
        "cancelled",
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return job


def _meeting(ctx, name, seconds, at, lines, embeddings, notes=""):
    s = ctx.sessions.create_session(name)
    folder = ctx.settings.recordings_dir / s.id
    audio = _audio(folder / "rec_mixed.ogg", seconds)
    mic = _audio(folder / "rec_device_1.ogg", seconds)
    rec = Recording(source="mic", device_id=1, device_name="Mic", path=str(mic), created_at=at)
    ctx.sessions.set_audio(s.id, str(audio), [rec])
    ctx.sessions.set_transcript(
        s.id,
        [TranscriptSegment(text=t, speaker=who, start=st, end=st + 0.4) for who, st, t in lines],
    )
    ctx.repo.set_session_embeddings(s.id, embeddings)
    if notes:
        ctx.sessions.update_notes(s.id, notes)
    return s.id


@needs_ffmpeg
def test_two_meetings_become_one_in_recording_order(client, ctx):
    t0 = datetime(2026, 9, 28, 10, 0)
    later = _meeting(
        ctx, "Planning", 3.0, t0 + timedelta(minutes=10),
        [("Ana", 0.5, "hello again"), ("SPEAKER_00", 1.5, "where were we")],
        {"Ana": [1.0, 0.0, 0.0], "SPEAKER_00": [0.0, 1.0, 0.0]},
        notes="Planning notes",
    )  # fmt: skip
    earlier = _meeting(
        ctx, "Planning (dropped)", 2.0, t0,
        [("SPEAKER_00", 0.2, "can you hear me"), ("SPEAKER_01", 1.0, "yes"),
         ("Bob", 1.5, "I am here")],
        {"SPEAKER_00": [0.0, 0.99, 0.1], "SPEAKER_01": [0.0, 0.0, 1.0]},
        notes="Before the drop",
    )  # fmt: skip
    ctx.repo.update_fields(earlier, local_only=True)
    clip = ctx.settings.recordings_dir / earlier / "clips" / "c1.ogg"
    clip.parent.mkdir()
    clip.write_bytes(b"clip")

    job = client.post(f"/api/sessions/{later}/combine", json={"other_id": earlier}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "completed", done["error"]
    assert done["result"]["parts"] == 2

    s = client.get(f"/api/sessions/{later}").json()
    assert s["name"] == "Planning"  # this meeting keeps its name and id
    assert audio_seconds(s["audio_file"], None) == pytest.approx(5.0, abs=0.15)
    parts = sorted(
        (r["part"], round(r["offset"], 1), Path(r["path"]).name) for r in s["recordings"]
    )
    assert parts == [(0, 0.0, f"{earlier}_rec_device_1.ogg"), (1, 2.0, "rec_device_1.ogg")]
    assert all(Path(r["path"]).parent.name == later for r in s["recordings"])
    lines = [(x["speaker"], round(x["start"], 1)) for x in s["transcript"]]
    # The earlier meeting first; its SPEAKER_00 is this one's by voice, SPEAKER_01 is new, and
    # Bob (named there, no voice print) stays Bob.
    assert lines == [
        ("SPEAKER_00", 0.2), ("SPEAKER_01", 1.0), ("Bob", 1.5),
        ("Ana", 2.5), ("SPEAKER_00", 3.5),
    ]  # fmt: skip
    assert "Planning notes" in s["notes"] and "From “Planning (dropped)”" in s["notes"]
    assert s["local_only"] is True  # the stricter privacy wins
    assert client.get(f"/api/sessions/{earlier}").status_code == 404
    assert (ctx.settings.recordings_dir / later / "clips" / "c1.ogg").read_bytes() == b"clip"
    assert not (ctx.settings.recordings_dir / earlier).exists()
    assert not (ctx.settings.recordings_dir / later / "rec_mixed.ogg").exists()  # joined now


def test_combine_needs_two_meetings_with_audio(client, ctx):
    a = ctx.sessions.create_session("a").id
    assert client.post(f"/api/sessions/{a}/combine", json={"other_id": a}).status_code == 400
    b = ctx.sessions.create_session("b").id
    assert client.post(f"/api/sessions/{a}/combine", json={"other_id": b}).status_code == 400
    assert client.post("/api/sessions/nope/combine", json={"other_id": a}).status_code == 404


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x10" * int(16000 * seconds))
    return buf.getvalue()


@needs_ffmpeg
def test_an_audio_file_is_added_as_the_next_part(client, ctx, fake_engine):
    sid = _meeting(ctx, "Standup", 5.0, datetime(2026, 9, 28, 9, 0), [("Ana", 0.5, "morning")], {})
    res = client.post(
        "/api/audio/import",
        files={"file": ("phone.wav", _wav(2.0), "audio/wav")},
        data={"session_id": sid},
    )
    assert res.status_code == 200, res.text
    assert res.json()["session"]["id"] == sid
    assert _wait(client, res.json()["job_id"])["status"] == "completed"
    s = client.get(f"/api/sessions/{sid}").json()
    assert audio_seconds(s["audio_file"], None) == pytest.approx(7.0, abs=0.15)
    assert sorted((r["part"], round(r["offset"], 1)) for r in s["recordings"]) == [
        (0, 0.0),
        (1, 5.0),
    ]
    assert len(fake_engine.sources) == 1  # only the new part was transcribed
    assert s["transcript"][0]["text"] == "morning"  # the first part's lines are kept
    assert all(x["start"] >= 5.0 for x in s["transcript"][1:])
