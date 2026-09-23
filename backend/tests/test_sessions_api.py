"""Session CRUD through the HTTP API."""


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"


def test_create_and_get_session(client):
    created = client.post("/api/sessions", json={"name": "Standup"}).json()
    assert created["name"] == "Standup"
    assert created["status"] == "created"
    assert created["transcript"] == []
    assert created["recordings"] == []
    assert client.get(f"/api/sessions/{created['id']}").json() == created


def test_list_sessions_summary_fields_and_order(client):
    a = client.post("/api/sessions", json={"name": "A"}).json()
    b = client.post("/api/sessions", json={"name": "B"}).json()
    listed = client.get("/api/sessions").json()
    ids = [s["id"] for s in listed]
    assert ids.index(b["id"]) < ids.index(a["id"])
    match = next(s for s in listed if s["id"] == a["id"])
    assert match == {
        "id": a["id"],
        "name": "A",
        "status": "created",
        "created_at": a["created_at"],
        "updated_at": a["updated_at"],
        "has_transcript": False,
        "has_summary": False,
        "has_audio": False,
        "participant_count": 0,
    }


def test_rename_and_notes(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert (
        client.patch(f"/api/sessions/{sid}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    )
    noted = client.post(f"/api/sessions/{sid}/notes", json={"notes": "hello"}).json()
    assert noted["notes"] == "hello"
    assert noted["name"] == "Renamed"


def test_delete_session_removes_recordings_dir(client, ctx):
    sid = client.post("/api/sessions", json={}).json()["id"]
    rec_dir = ctx.settings.recordings_dir / sid
    rec_dir.mkdir(parents=True)
    (rec_dir / "x.ogg").write_bytes(b"x")

    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404
    assert not rec_dir.exists()


def test_unknown_session_is_404(client):
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.patch("/api/sessions/nope", json={"name": "x"}).status_code == 404
    assert client.post("/api/sessions/nope/notes", json={"notes": "x"}).status_code == 404
    assert client.post("/api/sessions/nope/transcribe").status_code == 404


def test_session_events_are_broadcast(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        sid = client.post("/api/sessions", json={"name": "Evt"}).json()["id"]
        assert ws.receive_json() == {"type": "session", "session_id": sid, "status": "created"}
        client.delete(f"/api/sessions/{sid}")
        assert ws.receive_json() == {"type": "session", "session_id": sid, "status": "deleted"}
