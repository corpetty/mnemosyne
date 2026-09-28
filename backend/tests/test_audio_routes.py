"""Recording start/stop with PipeWire and ffmpeg replaced by fakes."""

from tests.conftest import drain_until_job, stop_and_finish


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
