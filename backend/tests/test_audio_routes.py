"""Recording start/stop with PipeWire and ffmpeg replaced by fakes."""

import pytest

from tests.conftest import drain_until_job, stop_and_finish
from tests.test_parts import needs_ffmpeg, real_audio  # noqa: F401 (a fixture)


def test_start_requires_devices(client, fake_pipewire):
    assert client.post("/api/audio/start", json={"device_ids": []}).status_code == 400


def test_start_stop_records_sources_and_queues_job(client, ctx, fake_pipewire, fake_engine):
    started = client.post("/api/audio/start", json={"device_ids": [1, 2]}).json()
    sid = started["session_id"]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "recording"
    assert client.get(f"/api/audio/status/{sid}").json()["device_count"] == 2
    assert (
        client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid}).status_code
        == 409
    )

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        stopped = client.post(f"/api/audio/stop/{sid}").json()
        # Answered at once: capture has stopped, the rest is the finish job.
        assert stopped["session"]["status"] == "encoding" and stopped["will_transcribe"]
        assert client.get(f"/api/audio/status/{sid}").json()["exists"] is False
        drain_until_job(ws, stopped["job_id"])
        finish = client.get(f"/api/jobs/{stopped['job_id']}").json()
        assert finish["kind"] == "finish" and finish["status"] == "completed"
        drain_until_job(ws, finish["result"]["transcribe_job_id"])

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["audio_file"].endswith("rec00001_mixed.ogg")
    assert [(r["source"], r["device_name"]) for r in session["recordings"]] == [
        ("mic", "Built-in Mic"),
        ("system", "Speakers"),
    ]
    # Per-source transcription: mic labelled as the local speaker, system diarized.
    assert fake_engine.transcribed_paths == [r["path"] for r in session["recordings"]]
    assert [(s.kind, s.speaker_label) for s in fake_engine.sources[0]] == [
        ("mic", "Me"),
        ("system", None),
    ]
    assert session["status"] == "completed"


def test_stop_without_transcribe(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    stopped, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert stopped["will_transcribe"] is False
    assert finish["result"]["transcribe_job_id"] is None
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "created"
    assert [j for j in client.get("/api/jobs").json() if j["kind"] == "transcribe"] == []


def test_auto_transcribe_setting_respected(client, ctx, fake_pipewire):
    ctx.settings.auto_transcribe = False
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    stopped, finish = stop_and_finish(client, sid)
    assert stopped["will_transcribe"] is False and finish["result"]["transcribe_job_id"] is None


def test_a_recording_without_audio_ends_in_error(client, ctx, fake_pipewire, monkeypatch):
    from mnemosyne.api.routes import audio as audio_routes

    async def nothing(session):
        return []

    monkeypatch.setattr(audio_routes, "stop_recording", nothing)
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    _, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert finish["status"] == "failed" and "no audio" in finish["error"]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "error"


def test_stop_unknown_is_404(client, fake_pipewire):
    assert client.post("/api/audio/stop/nope").status_code == 404


def test_stop_answers_before_the_slow_part(client, ctx, fake_pipewire, monkeypatch):
    """Encoding a long meeting takes many seconds; the Stop button must not wait for it."""
    import asyncio
    import time

    from mnemosyne.api.routes import audio as audio_routes

    slow_stop = audio_routes.stop_recording

    async def slow_encode(session):
        await asyncio.sleep(2)
        return await slow_stop(session)

    monkeypatch.setattr(audio_routes, "stop_recording", slow_encode)
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    t = time.monotonic()
    stopped = client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
    assert stopped.status_code == 200 and time.monotonic() - t < 0.5
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "encoding"
    deadline = time.monotonic() + 5
    while client.get(f"/api/sessions/{sid}").json()["status"] == "encoding":
        assert time.monotonic() < deadline, "the finish job never completed"
        time.sleep(0.1)
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "created"


# ---- the part clock (time chips in the notes, guests' META) ----------------------------------


def _iso_seconds(iso: str) -> float:
    from datetime import datetime

    at = datetime.fromisoformat(iso)
    assert at.utcoffset() is not None and at.utcoffset().total_seconds() == 0  # UTC
    return at.timestamp()


def test_the_first_recording_has_a_clock_at_part_0(client, ctx, fake_pipewire):
    from mnemosyne.services.parts import recording_clock

    started = client.post("/api/audio/start", json={"device_ids": [1]}).json()
    sid = started["session_id"]
    clock = started["clock"]
    assert clock["part"] == 0 and clock["offset"] == 0.0
    assert _iso_seconds(clock["started_at"]) == pytest.approx(
        ctx.active_recordings[sid].started_at, abs=0.001
    )
    assert recording_clock(ctx, sid) == clock
    (active,) = client.get("/api/audio/active").json()
    assert active["clock"] == clock
    stop_and_finish(client, sid, {"transcribe": False})
    # Stopped: no clock, and nothing listed as recording.
    assert recording_clock(ctx, sid) is None
    assert client.get("/api/audio/active").json() == []


@needs_ffmpeg
@pytest.mark.usefixtures("real_audio")
def test_a_restart_and_a_new_recording_are_new_parts_on_the_clock(client, ctx):
    """The clock's part and offset are what the save at stop gives the part (add_part)."""
    from mnemosyne.services.parts import recording_clock

    started = client.post("/api/audio/start", json={"device_ids": [1]}).json()
    sid = started["session_id"]
    first = started["clock"]
    # The capture is restarted: part 0 (5 s) is saved, part 1 starts where it ends.
    res = client.post(f"/api/audio/restart/{sid}")
    assert res.status_code == 200, res.text
    second = res.json()["clock"]
    assert second["part"] == 1 and second["offset"] == pytest.approx(5.0, abs=0.15)
    assert _iso_seconds(second["started_at"]) >= _iso_seconds(first["started_at"])
    assert client.get("/api/audio/active").json()[0]["clock"] == second
    _, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert finish["status"] == "completed", finish["error"]
    assert recording_clock(ctx, sid) is None
    # Recording into the meeting again: part 2, after parts 0 and 1 (5 s + 2 s).
    third = client.post("/api/audio/start", json={"device_ids": [1], "session_id": sid}).json()
    assert third["clock"]["part"] == 2
    assert third["clock"]["offset"] == pytest.approx(7.0, abs=0.2)
    _, finish = stop_and_finish(client, sid, {"transcribe": False})
    assert finish["status"] == "completed", finish["error"]
    recordings = client.get(f"/api/sessions/{sid}").json()["recordings"]
    saved = {r["part"]: r["offset"] for r in recordings}
    assert saved[1] == pytest.approx(second["offset"], abs=0.01)
    assert saved[2] == pytest.approx(third["clock"]["offset"], abs=0.01)
