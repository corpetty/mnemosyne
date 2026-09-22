"""WebSocket transcription flow using the FakeEngine (no torch, no CUDA)."""

from tests.fakes import FAKE_SEGMENTS, FakeEngine


def _drain(ws):
    """Collect messages until a terminal status or error."""
    messages = []
    while True:
        msg = ws.receive_json()
        messages.append(msg)
        if msg["type"] == "error":
            return messages
        if msg["type"] == "status" and msg["message"] == "Transcription complete":
            return messages


def test_ping_pong(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_transcribe_streams_segments_and_persists(client, fake_engine):
    sid = client.post("/api/sessions", json={"name": "WS"}).json()["id"]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "transcribe", "audio_path": "/tmp/x.ogg", "session_id": sid})
        messages = _drain(ws)

    statuses = [m["message"] for m in messages if m["type"] == "status"]
    assert statuses == ["Loading models...", "Transcribing...", "Transcription complete"]

    segments = [m["segment"] for m in messages if m["type"] == "transcription"]
    assert [s["text"] for s in segments] == [s.text for s in FAKE_SEGMENTS]
    assert all(m["session_id"] == sid for m in messages)

    assert fake_engine.loaded is True
    assert fake_engine.transcribed_paths == ["/tmp/x.ogg"]

    session = client.get(f"/api/sessions/{sid}").json()
    assert session["status"] == "completed"
    assert len(session["transcript"]) == len(FAKE_SEGMENTS)
    assert session["participants"] == ["SPEAKER_00", "SPEAKER_01"]


def test_transcribe_without_session_does_not_persist(client, fake_engine):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "transcribe", "audio_path": "/tmp/y.ogg"})
        messages = _drain(ws)
    assert sum(1 for m in messages if m["type"] == "transcription") == len(FAKE_SEGMENTS)
    assert all(m["session_id"] is None for m in messages)


def test_engine_failure_is_reported_as_error(client, monkeypatch):
    from src.mnemosyne.services import model_service as mod

    monkeypatch.setattr(mod.model_service, "_engine", FakeEngine(fail=True))
    sid = client.post("/api/sessions", json={}).json()["id"]

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "transcribe", "audio_path": "/tmp/z.ogg", "session_id": sid})
        messages = _drain(ws)

    assert messages[-1]["type"] == "error"
    assert "fake engine failure" in messages[-1]["message"]
    assert client.get(f"/api/sessions/{sid}").json()["transcript"] == []
