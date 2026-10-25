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


def _two_parts(ctx, name, at0, at1, notes):
    """A meeting of two 2 s parts (the second at 2 s), recorded at `at0` and `at1`."""
    s = ctx.sessions.create_session(name)
    folder = ctx.settings.recordings_dir / s.id
    audio = _audio(folder / "rec_mixed.ogg", 4.0)
    recs = []
    for part, at in ((0, at0), (1, at1)):
        mic = _audio(folder / f"rec_device_{part}.ogg", 2.0)
        recs.append(
            Recording(source="mic", device_id=1, device_name="Mic", path=str(mic),
                      created_at=at, part=part, offset=2.0 * part)
        )  # fmt: skip
    ctx.sessions.set_audio(s.id, str(audio), recs)
    ctx.sessions.set_transcript(
        s.id,
        [
            TranscriptSegment(text="one", speaker="Ana", start=0.5, end=0.9),
            TranscriptSegment(text="two", speaker="Ana", start=2.5, end=2.9),
        ],
    )
    ctx.sessions.update_notes(s.id, notes)
    return s.id


@needs_ffmpeg
def test_chips_follow_the_parts_both_meetings_are_numbered_anew(client, ctx):
    t0 = datetime(2026, 9, 28, 10, 0)
    # Recording order: A0, B0, A1, B1, so the new numbers are A0=0 B0=1 A1=2 B1=3.
    a = _two_parts(
        ctx, "Alpha", t0, t0 + timedelta(minutes=20),
        "Start [0:05](mnemosyne:t/0/5), later [0:40](mnemosyne:t/1/40).\n\n"
        "See [Beta, Sep 28](mnemosyne:meeting/{b}) and [Other](https://example.com/mnemosyne:t/1/9)"
        " and [@Ada](mnemosyne:person/Ada) and [x](mnemosyne:t/7/1).",
    )  # fmt: skip
    b = _two_parts(
        ctx, "Beta", t0 + timedelta(minutes=10), t0 + timedelta(minutes=30),
        "Beta [1:00](mnemosyne:t/0/60) then [2:00](mnemosyne:t/1/120); `[c](mnemosyne:t/1/3)`.\n"
        "Back to [Alpha](mnemosyne:meeting/{a}).",
    )  # fmt: skip
    # The ids are only known now: write the chips that point at the other meeting.
    ctx.sessions.update_notes(a, ctx.sessions.get_session(a).notes.replace("{b}", b))
    ctx.sessions.update_notes(b, ctx.sessions.get_session(b).notes.replace("{a}", a))

    job = client.post(f"/api/sessions/{a}/combine", json={"other_id": b}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "completed", done["error"]
    assert done["result"]["parts"] == 4

    notes = client.get(f"/api/sessions/{a}").json()["notes"]
    mine, theirs = notes.split("## From “Beta”")
    assert "[0:05](mnemosyne:t/0/5)" in mine  # A0 stays 0
    assert "[0:40](mnemosyne:t/2/40)" in mine  # A1 is the third part now, seconds unchanged
    assert f"[Beta, Sep 28](mnemosyne:meeting/{a})" in mine  # the removed meeting is this one
    assert "(https://example.com/mnemosyne:t/1/9)" in mine  # another link
    assert "[@Ada](mnemosyne:person/Ada)" in mine
    assert "[x](mnemosyne:t/7/1)" in mine  # a part we do not know stays
    assert "[1:00](mnemosyne:t/1/60)" in theirs  # B0 is part 1
    assert "[2:00](mnemosyne:t/3/120)" in theirs  # B1 is part 3
    assert "`[c](mnemosyne:t/1/3)`" in theirs  # code is not a link
    assert f"[Alpha](mnemosyne:meeting/{a})" in theirs
    assert f"mnemosyne:meeting/{b}" not in notes


@needs_ffmpeg
def test_chips_of_the_target_are_renumbered_even_when_the_other_has_no_notes(client, ctx):
    t0 = datetime(2026, 9, 28, 10, 0)
    a = _two_parts(
        ctx, "Alpha", t0 + timedelta(minutes=10), t0 + timedelta(minutes=30),
        "[a](mnemosyne:t/0/1) [b](mnemosyne:t/1/2)",
    )  # fmt: skip
    b = _two_parts(ctx, "Beta", t0, t0 + timedelta(minutes=20), "")
    done = _wait(
        client, client.post(f"/api/sessions/{a}/combine", json={"other_id": b}).json()["id"]
    )
    assert done["status"] == "completed", done["error"]
    # Order: B0, A0, B1, A1.
    notes = client.get(f"/api/sessions/{a}").json()["notes"]
    assert notes.strip() == "[a](mnemosyne:t/1/1) [b](mnemosyne:t/3/2)"


def test_retarget_chips_changes_only_the_url():
    from mnemosyne.services.notes_chips import retarget_chips

    md = "[0:05](mnemosyne:t/0/5) [Plan](<mnemosyne:meeting/abcdef12>) [@A](mnemosyne:person/A)"
    out = retarget_chips(md, lambda kind, v: "9/5" if kind == "time" else "00ff00ff")
    assert (
        out == "[0:05](mnemosyne:t/9/5) [Plan](mnemosyne:meeting/00ff00ff) [@A](mnemosyne:person/A)"
    )
    assert retarget_chips(md, lambda kind, v: None) == md


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
