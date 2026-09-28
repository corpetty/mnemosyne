"""Recording again into a meeting adds a part instead of replacing it (services/parts.py)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from mnemosyne.models.session import Recording, Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.parts import match_speakers, next_part, part_of, shift
from tests.conftest import stop_and_finish

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def test_speakers_of_a_later_part_keep_their_labels():
    known = {"SPEAKER_00": [1.0, 0.0, 0.0], "Ana": [0.0, 1.0, 0.0]}
    new = {"SPEAKER_00": [0.1, 0.99, 0.0], "SPEAKER_01": [0.98, 0.05, 0.0], "SPEAKER_02": [0, 0, 1]}
    mapping = match_speakers(new, known, taken={"SPEAKER_00", "SPEAKER_03"}, threshold=0.6)
    assert mapping == {"SPEAKER_00": "Ana", "SPEAKER_01": "SPEAKER_00", "SPEAKER_02": "SPEAKER_04"}


def test_one_known_voice_is_not_given_to_two_new_labels():
    known = {"SPEAKER_00": [1.0, 0.0]}
    new = {"SPEAKER_00": [0.9, 0.1], "SPEAKER_01": [0.95, 0.05]}
    mapping = match_speakers(new, known, taken={"SPEAKER_00"}, threshold=0.6)
    assert sorted(mapping.values()) == ["SPEAKER_00", "SPEAKER_01"]
    assert mapping["SPEAKER_01"] == "SPEAKER_00"  # the closer one wins


def test_timeline_helpers():
    seg = TranscriptSegment(text="x", speaker="A", start=1.0, end=2.0)
    assert shift([seg], 60)[0].start == 61.0 and shift([seg], 0)[0] is seg
    starts = {0: 0.0, 1: 300.0}
    assert part_of(10, starts) == 0 and part_of(300, starts) == 1 and part_of(999, starts) == 1
    empty = Session(name="m")
    assert next_part(empty) == 0
    two = Session(
        name="m",
        audio_file="/x.ogg",
        recordings=[Recording(source="mic", device_id=1, device_name="m", path="/a", part=1)],
    )
    assert next_part(two) == 2


def _audio(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=300:d={seconds}",
         "-c:a", "libopus", str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture
def real_audio(fake_pipewire, monkeypatch):
    """Recordings produce real (short) audio, so parts can be measured and joined."""
    from mnemosyne.api.routes import audio as audio_routes

    lengths = iter([5.0, 2.0, 2.0, 2.0])  # part 0 outlasts the fake transcript (4.5 s)

    async def stop_recording(session):
        session.is_recording = False
        return [_audio(p.output_path.with_suffix(".ogg"), next(lengths)) for p in session.processes]

    def mix(inputs, output):
        out = Path(output).with_suffix(".ogg")
        shutil.copy(inputs[0], out)
        return out

    from mnemosyne.audio.capture import RecordingProcess, RecordingSession

    count = iter(range(1, 100))

    async def start_recording(device_ids, output_dir, **_):  # unique names, like real ones
        output_dir.mkdir(parents=True, exist_ok=True)
        rec = f"rec{next(count):05d}"
        session = RecordingSession(session_id=rec, output_dir=output_dir)

        class Done:
            returncode = 0

        session.processes = [
            RecordingProcess(device_id=d, process=Done(), output_path=output_dir / f"{rec}_{d}.wav")
            for d in device_ids
        ]
        session.is_recording = True
        return session

    monkeypatch.setattr(audio_routes, "start_recording", start_recording)
    monkeypatch.setattr(audio_routes, "stop_recording", stop_recording)
    monkeypatch.setattr(audio_routes, "mix_audio_files", mix)


def _wait(client, job_id, timeout=20):
    import time

    deadline = time.monotonic() + timeout
    while (job := client.get(f"/api/jobs/{job_id}").json())["status"] not in (
        "completed",
        "failed",
        "cancelled",
    ):
        assert time.monotonic() < deadline, f"job {job_id} did not finish"
        time.sleep(0.05)
    return job


def _record(client, sid=None):
    body = {"device_ids": [1]} | ({"session_id": sid} if sid else {})
    sid = client.post("/api/audio/start", json=body).json()["session_id"]
    return sid


@needs_ffmpeg
def test_recording_again_adds_a_part_and_transcribes_only_it(client, ctx, real_audio, fake_engine):
    ctx.settings.auto_summarize = False
    fake_engine.last_speaker_embeddings = {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}
    sid = _record(client)
    _, finish = stop_and_finish(client, sid)
    _wait(client, finish["result"]["transcribe_job_id"])
    first = client.get(f"/api/sessions/{sid}").json()
    assert len(first["transcript"]) == 3
    client.post(
        f"/api/sessions/{sid}/speakers/rename",
        json={"label": "SPEAKER_01", "name": "Ana", "enroll": False},
    )

    # A second recording into the same meeting (after a crash, a pause...).
    fake_engine.sources.clear()
    fake_engine.last_speaker_embeddings = {"SPEAKER_00": [0.1, 0.99], "SPEAKER_01": [0.99, 0.1]}
    assert _record(client, sid) == sid
    _, finish = stop_and_finish(client, sid)
    assert finish["status"] == "completed", finish["error"]
    assert _wait(client, finish["result"]["transcribe_job_id"])["status"] == "completed"

    session = client.get(f"/api/sessions/{sid}").json()
    parts = [(r["part"], round(r["offset"], 1)) for r in session["recordings"]]
    assert parts == [(0, 0.0), (1, 5.0)]  # part 1 starts where part 0's 5 s end
    assert Path(session["audio_file"]).name == "meeting_1.ogg"
    from mnemosyne.services.parts import audio_seconds

    assert audio_seconds(session["audio_file"], None) == pytest.approx(7.0, abs=0.15)
    # Only the new part went to the engine; the first part's lines are kept as they were.
    assert len(fake_engine.sources) == 1
    (source,) = fake_engine.sources[0]  # one mic: part 1's stretch of the meeting audio
    assert source.kind == "mixed" and source.path.endswith("-part1.ogg")
    assert not Path(source.path).exists()  # the private cut is removed afterwards
    lines = [(s["speaker"], round(s["start"], 1)) for s in session["transcript"]]
    assert lines[:3] == [("SPEAKER_00", 0.0), ("Ana", 1.5), ("SPEAKER_00", 3.2)]
    # Part 1's voices matched part 0's: its SPEAKER_00 is Ana, its SPEAKER_01 is SPEAKER_00.
    assert lines[3:] == [("Ana", 5.0), ("SPEAKER_00", 6.5), ("Ana", 8.2)]


@needs_ffmpeg
def test_recovery_after_a_crash_adds_to_the_meeting(settings, keystore, real_audio, fake_engine):
    """A crash during the second recording must not throw away the first."""
    import json
    import wave

    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app
    from mnemosyne.models.session import SessionStatus
    from mnemosyne.services.parts import audio_seconds

    app = create_app(settings, keystore=keystore)
    app.state.ctx.settings.auto_transcribe = False
    with TestClient(app) as client:
        sid = _record(client)
        stop_and_finish(client, sid, {"transcribe": False})
        before = client.get(f"/api/sessions/{sid}").json()
        # A second recording starts, then the app dies: its WAV and manifest stay behind.
        ctx = app.state.ctx
        folder = ctx.settings.recordings_dir / sid
        with wave.open(str(folder / "crash_device_1.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x10" * 16000 * 2)
        track = {"device_id": 1, "device_name": "Mic", "source": "mic", "wav": "crash_device_1.wav"}
        (folder / "recording.json").write_text(
            json.dumps({"recording_id": "crash", "part": 1, "tracks": [track]})
        )
        ctx.sessions.set_status(sid, SessionStatus.RECORDING)

    restarted = create_app(settings, keystore=keystore)  # the next start recovers it
    restarted.state.ctx.settings.auto_transcribe = False
    with TestClient(restarted) as client:
        (job,) = [j for j in client.get("/api/jobs").json() if j["kind"] == "recover"]
        assert _wait(client, job["id"])["status"] == "completed"
        after = client.get(f"/api/sessions/{sid}").json()
    assert [r["part"] for r in after["recordings"]] == [0, 1]
    assert after["recordings"][0]["path"] == before["recordings"][0]["path"]
    assert audio_seconds(after["audio_file"], None) == pytest.approx(7.0, abs=0.2)
