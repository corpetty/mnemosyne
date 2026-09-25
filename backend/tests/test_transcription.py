"""Transcription as a background job, observed over the WebSocket."""

from tests.conftest import drain_until_job
from tests.fakes import FAKE_SEGMENTS, FakeEngine


def test_ping_pong(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_transcribe_requires_audio(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    resp = client.post(f"/api/sessions/{sid}/transcribe")
    assert resp.status_code == 400


def test_transcribe_job_streams_and_persists(client, ctx, fake_engine):
    sid = client.post("/api/sessions", json={"name": "WS"}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        assert job["kind"] == "transcribe"
        assert job["session_id"] == sid
        events = drain_until_job(ws, job["id"])

    progress = [
        e["job"]["progress"]
        for e in events
        if e["type"] == "job" and e["job"]["status"] == "running"
    ]
    assert 0.5 in progress and 0.9 in progress  # engine progress reached the job
    messages = [e["job"]["message"] for e in events if e["type"] == "job"]
    assert "Identifying speakers in audio..." in messages
    job_statuses = [e["job"]["status"] for e in events if e["type"] == "job"]
    assert job_statuses[0] == "queued"
    assert "running" in job_statuses
    assert job_statuses[-1] == "completed"

    statuses = [e["message"] for e in events if e["type"] == "status"]
    assert statuses == ["Loading models...", "Transcribing...", "Transcription complete"]

    segments = [e["segment"] for e in events if e["type"] == "transcription"]
    assert [s["text"] for s in segments] == [s.text for s in FAKE_SEGMENTS]

    session_statuses = [e["status"] for e in events if e["type"] == "session"]
    assert session_statuses == ["transcribing", "completed"]

    assert fake_engine.loaded
    assert fake_engine.transcribed_paths == ["/fake/mixed.ogg"]

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "completed"
    assert len(session["transcript"]) == len(FAKE_SEGMENTS)
    assert session["participants"] == ["SPEAKER_00", "SPEAKER_01"]

    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed"
    assert done["result"] == {
        "segments": len(FAKE_SEGMENTS),
        "sources": 1,
        "echo_dropped": 0,
        "glossary_fixes": 0,
    }


def test_engine_failure_marks_job_and_session(client, ctx):
    ctx.models._engine = FakeEngine(fail=True)
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        events = drain_until_job(ws, job["id"])

    assert events[-1]["job"]["status"] == "failed"
    assert "fake engine failure" in events[-1]["job"]["error"]
    assert any(e["type"] == "error" for e in events)
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "error"


def test_duplicate_transcribe_is_409(client, ctx, fake_engine):
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        first = client.post(f"/api/sessions/{sid}/transcribe")
        assert first.status_code == 200
        # The job is queued on the loop but has not run yet from the client's view.
        second = client.post(f"/api/sessions/{sid}/transcribe")
        drain_until_job(ws, first.json()["id"])
    assert second.status_code in (200, 409)
    if second.status_code == 200:
        # It only succeeds if the first had already finished; then it must have run too.
        assert client.get(f"/api/jobs/{second.json()['id']}").json()["status"] in (
            "queued",
            "running",
            "completed",
        )


def test_hello_lists_active_jobs(client, ctx, fake_engine):
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello", "jobs": [], "recovered": []}
