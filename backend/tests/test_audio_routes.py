"""Recording start/stop with PipeWire and ffmpeg replaced by fakes."""

from tests.conftest import drain_until_job


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
        assert stopped["job_id"]
        drain_until_job(ws, stopped["job_id"])

    session = stopped["session"]
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
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "completed"
    assert client.get(f"/api/audio/status/{sid}").json()["exists"] is False


def test_stop_without_transcribe(client, ctx, fake_pipewire):
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    stopped = client.post(f"/api/audio/stop/{sid}", json={"transcribe": False}).json()
    assert stopped["job_id"] is None
    assert stopped["session"]["status"] == "created"
    assert [j for j in client.get("/api/jobs").json() if j["kind"] == "transcribe"] == []


def test_auto_transcribe_setting_respected(client, ctx, fake_pipewire):
    ctx.settings.auto_transcribe = False
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    assert client.post(f"/api/audio/stop/{sid}").json()["job_id"] is None


def test_stop_unknown_is_404(client, fake_pipewire):
    assert client.post("/api/audio/stop/nope").status_code == 404
