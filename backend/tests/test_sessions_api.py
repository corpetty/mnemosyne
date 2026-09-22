"""Session CRUD through the HTTP API."""


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_session(client):
    created = client.post("/api/sessions", json={"name": "Standup"}).json()
    assert created["name"] == "Standup"
    assert created["status"] == "created"
    assert created["transcript"] == []

    fetched = client.get(f"/api/sessions/{created['id']}").json()
    assert fetched == created


def test_list_sessions_includes_summary_fields(client):
    created = client.post("/api/sessions", json={"name": "Listed"}).json()
    listed = client.get("/api/sessions").json()
    match = next(s for s in listed if s["id"] == created["id"])
    assert match["has_transcript"] is False
    assert match["has_summary"] is False
    assert match["participant_count"] == 0


def test_list_sessions_sorted_newest_first(client):
    a = client.post("/api/sessions", json={"name": "A"}).json()
    b = client.post("/api/sessions", json={"name": "B"}).json()
    ids = [s["id"] for s in client.get("/api/sessions").json()]
    assert ids.index(b["id"]) < ids.index(a["id"])


def test_rename_and_notes(client):
    sid = client.post("/api/sessions", json={}).json()["id"]

    renamed = client.patch(f"/api/sessions/{sid}", json={"name": "Renamed"}).json()
    assert renamed["name"] == "Renamed"

    noted = client.post(f"/api/sessions/{sid}/notes", json={"notes": "hello"}).json()
    assert noted["notes"] == "hello"
    assert noted["name"] == "Renamed"


def test_delete_session(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_unknown_session_is_404(client):
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.patch("/api/sessions/nope", json={"name": "x"}).status_code == 404
    assert client.post("/api/sessions/nope/notes", json={"notes": "x"}).status_code == 404
