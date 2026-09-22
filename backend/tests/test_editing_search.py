"""Transcript editing endpoints and full-text search."""

from src.mnemosyne.models.transcript import TranscriptSegment, WordSegment
from src.mnemosyne.storage.sqlite import fts_query

from tests.conftest import drain_until_job
from tests.fakes import FakeEngine, FakeTranscriber  # noqa: F401


def _session(client, ctx, segments):
    engine = FakeEngine(segments=segments)
    ctx.models._engine = engine
    sid = client.post("/api/sessions", json={"name": "Planning sync"}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    return sid


SEGS = [
    TranscriptSegment(
        text="We will ship the release on Friday",
        speaker="SPEAKER_00",
        start=0.0,
        end=4.0,
        words=[
            WordSegment(word=w, start=i * 0.5, end=i * 0.5 + 0.4)
            for i, w in enumerate("We will ship the release on Friday".split())
        ],
    ),
    TranscriptSegment(text="Sounds good to me", speaker="SPEAKER_01", start=4.5, end=6.0),
    TranscriptSegment(text="Let's also update the docs", speaker="SPEAKER_00", start=6.5, end=8.0),
]


def test_update_text_and_speaker(client, ctx):
    sid = _session(client, ctx, SEGS)
    s = client.patch(f"/api/sessions/{sid}/segments/1", json={"text": "Sounds great to me"}).json()
    assert s["transcript"][1]["text"] == "Sounds great to me"
    s = client.patch(f"/api/sessions/{sid}/segments/1", json={"speaker": "Alice"}).json()
    assert s["transcript"][1]["speaker"] == "Alice"
    assert s["participants"] == ["SPEAKER_00", "Alice"]
    # editing text drops stale word timings
    s = client.patch(f"/api/sessions/{sid}/segments/0", json={"text": "We ship Friday"}).json()
    assert s["transcript"][0]["words"] is None
    assert client.patch(f"/api/sessions/{sid}/segments/0", json={"text": " "}).status_code == 400
    assert client.patch(f"/api/sessions/{sid}/segments/9", json={"text": "x"}).status_code == 404
    assert client.patch("/api/sessions/nope/segments/0", json={"text": "x"}).status_code == 404


def test_delete_merge_split(client, ctx):
    sid = _session(client, ctx, SEGS)
    s = client.delete(f"/api/sessions/{sid}/segments/1").json()
    assert [x["text"] for x in s["transcript"]] == [SEGS[0].text, SEGS[2].text]
    assert s["participants"] == ["SPEAKER_00"]

    s = client.post(f"/api/sessions/{sid}/segments/1/merge").json()
    assert len(s["transcript"]) == 1
    merged = s["transcript"][0]
    assert merged["text"] == f"{SEGS[0].text} {SEGS[2].text}"
    assert merged["end"] == 8.0
    assert client.post(f"/api/sessions/{sid}/segments/0/merge").status_code == 400

    offset = len(SEGS[0].text)
    s = client.post(f"/api/sessions/{sid}/segments/0/split", json={"offset": offset}).json()
    assert [x["text"] for x in s["transcript"]] == [SEGS[0].text, SEGS[2].text]
    head, tail = s["transcript"]
    assert head["end"] == tail["start"]
    assert head["words"] and len(head["words"]) == 7 and tail["words"] is None
    assert (
        client.post(f"/api/sessions/{sid}/segments/0/split", json={"offset": 0}).status_code == 400
    )


def test_fts_query_builder():
    assert fts_query("ship friday") == '"ship" "friday"*'
    assert fts_query("  don't   panic ") == '"don\'t" "panic"*'
    assert fts_query('"; DROP TABLE') == '"DROP" "TABLE"*'
    assert fts_query("!!!") == ""


def test_search_segments_and_sessions(client, ctx):
    sid = _session(client, ctx, SEGS)
    other = client.post("/api/sessions", json={"name": "Budget review"}).json()["id"]
    client.post(f"/api/sessions/{other}/notes", json={"notes": "release budget approved"})

    hits = client.get("/api/search", params={"q": "release"}).json()
    ids = [h["session_id"] for h in hits]
    assert set(ids) == {sid, other}
    planning = next(h for h in hits if h["session_id"] == sid)
    assert planning["session_name"] == "Planning sync"
    assert planning["segments"][0]["idx"] == 0
    assert "[[release]]" in planning["segments"][0]["snippet"]
    assert planning["segments"][0]["speaker"] == "SPEAKER_00"
    budget = next(h for h in hits if h["session_id"] == other)
    assert "[[release]]" in budget["session_snippet"]

    # prefix match while typing; edits are reflected
    assert client.get("/api/search", params={"q": "fri"}).json()[0]["session_id"] == sid
    client.patch(f"/api/sessions/{sid}/segments/0", json={"text": "We ship Monday"})
    assert client.get("/api/search", params={"q": "friday"}).json() == []
    assert client.get("/api/search", params={"q": "monday"}).json()[0]["session_id"] == sid

    # deleting a session removes its index entries
    client.delete(f"/api/sessions/{sid}")
    assert client.get("/api/search", params={"q": "monday"}).json() == []
    assert client.get("/api/search", params={"q": "!!!"}).json() == []


def test_fts_backfill_for_old_databases(tmp_path):
    import sqlite3

    from src.mnemosyne.storage.sqlite import SessionRepository

    repo = SessionRepository(tmp_path / "db.sqlite")
    repo.close()
    # Simulate a pre-FTS database: drop the FTS tables, insert rows directly.
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.executescript(
        "DROP TRIGGER segments_ai; DROP TRIGGER sessions_ai; DROP TABLE segments_fts;"
        " DROP TABLE sessions_fts;"
        " INSERT INTO sessions(id,name,status,created_at,updated_at) VALUES"
        " ('old1','Legacy','completed','2026-01-01T00:00:00','2026-01-01T00:00:00');"
        ' INSERT INTO segments(session_id,idx,text,speaker,start,"end") VALUES'
        " ('old1',0,'ancient wisdom','S',0,1);"
    )
    conn.commit()
    conn.close()
    repo = SessionRepository(tmp_path / "db.sqlite")
    hits = repo.search("ancient")
    assert [h.session_id for h in hits] == ["old1"] and hits[0].segments[0].idx == 0
    repo.close()
