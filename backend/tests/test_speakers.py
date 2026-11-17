"""Voice profiles: matching, enrolling on rename, API, pipeline auto-labelling."""

import pytest

from mnemosyne.services.speaker_service import SpeakerService, cosine
from mnemosyne.storage.sqlite import SessionRepository
from mnemosyne.transcription.composed import ComposedEngine
from mnemosyne.transcription.engine import AudioSource
from tests.conftest import drain_until_job
from tests.fakes import FAKE_SEGMENTS, FakeDiarizer, FakeEngine, FakeTranscriber

A = [1.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0]
A_ISH = [0.9, 0.1, 0.0]


def test_cosine():
    assert cosine(A, A) == pytest.approx(1.0)
    assert cosine(A, B) == pytest.approx(0.0)
    assert cosine(A, [0.0, 0.0, 0.0]) == -1.0
    assert cosine(A, [1.0]) == -1.0


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "db.sqlite")
    yield r
    r.close()


def test_upsert_running_mean_and_rename_delete(repo):
    p = repo.upsert_speaker_sample("Alice", [1.0, 0.0])
    assert p.sample_count == 1
    p = repo.upsert_speaker_sample("Alice", [0.0, 1.0])
    assert p.sample_count == 2 and p.voices == {"pyannote": [0.5, 0.5]}
    assert [x.name for x in repo.list_speakers()] == ["Alice"]
    assert repo.rename_speaker(p.id, "Alice B").name == "Alice B"
    assert repo.get_speaker_by_name("Alice B") is not None
    assert repo.delete_speaker(p.id) is True
    assert repo.delete_speaker(p.id) is False


def test_match_is_greedy_and_unique(repo):
    repo.upsert_speaker_sample("Alice", A)
    repo.upsert_speaker_sample("Bob", B)
    svc = SpeakerService(repo, threshold=0.6)
    mapping = svc.match({"SPEAKER_00": A_ISH, "SPEAKER_01": B, "SPEAKER_02": A})
    # SPEAKER_02 is the exact Alice; SPEAKER_00 is close but Alice is taken.
    assert mapping == {"SPEAKER_02": "Alice", "SPEAKER_01": "Bob"}
    assert svc.match({"S": [0.0, 0.0, 1.0]}) == {}
    assert svc.match({}) == {}


def _transcribed(client, ctx, engine):
    ctx.models._engine = engine
    sid = client.post("/api/sessions", json={"name": "S"}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        events = drain_until_job(ws, job["id"])
    return sid, events


def test_rename_relabels_and_enrolls(client, ctx):
    engine = FakeEngine()
    engine.last_speaker_embeddings = {"SPEAKER_00": A, "SPEAKER_01": B}
    sid, _ = _transcribed(client, ctx, engine)

    info = client.get(f"/api/sessions/{sid}/speakers").json()
    assert info == [
        {"label": "SPEAKER_00", "has_voice": True},
        {"label": "SPEAKER_01", "has_voice": True},
    ]

    resp = client.post(
        f"/api/sessions/{sid}/speakers/rename", json={"label": "SPEAKER_00", "name": "Alice"}
    )
    assert resp.status_code == 200
    session = resp.json()
    assert session["participants"] == ["Alice", "SPEAKER_01"]
    assert {s["speaker"] for s in session["transcript"]} == {"Alice", "SPEAKER_01"}
    profiles = client.get("/api/speakers").json()
    assert [(p["name"], p["sample_count"]) for p in profiles] == [("Alice", 1)]
    assert "embedding" not in profiles[0]

    # Renaming to an existing name merges the participant list.
    client.post(
        f"/api/sessions/{sid}/speakers/rename", json={"label": "SPEAKER_01", "name": "Alice"}
    )
    assert client.get(f"/api/sessions/{sid}").json()["participants"] == ["Alice"]
    assert client.get("/api/speakers").json()[0]["sample_count"] == 2


def test_rename_without_enroll_and_errors(client, ctx):
    engine = FakeEngine()
    engine.last_speaker_embeddings = {"SPEAKER_00": A}
    sid, _ = _transcribed(client, ctx, engine)
    client.post(
        f"/api/sessions/{sid}/speakers/rename",
        json={"label": "SPEAKER_00", "name": "Carol", "enroll": False},
    )
    assert client.get("/api/speakers").json() == []
    bad = client.post(f"/api/sessions/{sid}/speakers/rename", json={"label": "X", "name": " "})
    assert bad.status_code == 400
    missing = client.post("/api/sessions/nope/speakers/rename", json={"label": "X", "name": "Y"})
    assert missing.status_code == 404


def test_pipeline_auto_labels_known_voices(client, ctx):
    ctx.repo.upsert_speaker_sample("Alice", A)
    engine = FakeEngine()
    engine.last_speaker_embeddings = {"SPEAKER_00": A_ISH, "SPEAKER_01": B}
    sid, events = _transcribed(client, ctx, engine)
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["participants"] == ["Alice", "SPEAKER_01"]
    assert [s["speaker"] for s in session["transcript"]] == [
        "Alice" if s.speaker == "SPEAKER_00" else s.speaker for s in FAKE_SEGMENTS
    ]
    assert any(e.get("message") == "Recognized Alice" for e in events if e["type"] == "status")
    # stored embeddings follow the new label so a later rename still enrolls
    assert set(ctx.repo.get_session_embeddings(sid)) == {"Alice", "SPEAKER_01"}


def test_auto_label_can_be_disabled(client, ctx):
    ctx.settings.auto_label_speakers = False
    ctx.repo.upsert_speaker_sample("Alice", A)
    engine = FakeEngine()
    engine.last_speaker_embeddings = {"SPEAKER_00": A}
    sid, _ = _transcribed(client, ctx, engine)
    participants = client.get(f"/api/sessions/{sid}").json()["participants"]
    assert participants == ["SPEAKER_00", "SPEAKER_01"]


def test_profile_api(client, ctx):
    p = ctx.repo.upsert_speaker_sample("Alice", A)
    ctx.repo.upsert_speaker_sample("Bob", B)
    assert client.patch(f"/api/speakers/{p.id}", json={"name": "Bob"}).status_code == 409
    assert client.patch(f"/api/speakers/{p.id}", json={"name": " "}).status_code == 400
    assert client.patch(f"/api/speakers/{p.id}", json={"name": "Alicia"}).json()["name"] == "Alicia"
    assert client.delete(f"/api/speakers/{p.id}").status_code == 200
    assert client.delete(f"/api/speakers/{p.id}").status_code == 404
    assert [s["name"] for s in client.get("/api/speakers").json()] == ["Bob"]


@pytest.mark.anyio
async def test_composed_engine_collects_embeddings_and_drops_echo():
    t = FakeTranscriber()
    d = FakeDiarizer(embeddings={"SPEAKER_00": A, "SPEAKER_01": B})
    engine = ComposedEngine(t, d, echo_dedup=True)
    sources = [
        AudioSource(path="/mic.ogg", kind="mic", speaker_label="Me"),
        AudioSource(path="/sys.ogg", kind="system"),
    ]
    out = [s async for s in engine.transcribe_sources(sources)]
    # The fake transcriber returns identical text for both files at identical
    # times, so every mic segment is bleed and gets dropped.
    assert {s.speaker for s in out} == {"SPEAKER_00", "SPEAKER_01"}
    assert engine.last_dropped_echo == len(FAKE_SEGMENTS)
    assert engine.last_speaker_embeddings == {"SPEAKER_00": A, "SPEAKER_01": B}

    engine.echo_dedup = False
    out = [s async for s in engine.transcribe_sources(sources)]
    assert "Me" in {s.speaker for s in out} and engine.last_dropped_echo == 0


def test_speakers_reviewed_is_remembered(client, ctx):
    sid = client.post("/api/sessions", json={"name": "m"}).json()["id"]
    assert client.get(f"/api/sessions/{sid}").json()["speakers_reviewed"] is False
    body = client.post(f"/api/sessions/{sid}/speakers/reviewed", json={}).json()
    assert body["speakers_reviewed"] is True
    assert ctx.repo.get(sid).speakers_reviewed is True
    assert client.post("/api/sessions/nope/speakers/reviewed", json={}).status_code == 404


def test_a_new_transcript_asks_again(client, ctx, transcribed_session):
    from tests.conftest import drain_until_job

    sid = transcribed_session["id"]
    assert transcribed_session["speakers_reviewed"] is False
    client.post(f"/api/sessions/{sid}/speakers/reviewed", json={})
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    assert ctx.repo.get(sid).speakers_reviewed is False  # new labels to name


# ---- voice spaces ----------------------------------------------------------


def test_a_profile_keeps_one_running_mean_per_space(repo):
    repo.upsert_speaker_sample("Alice", [1.0, 0.0], "pyannote")
    repo.upsert_speaker_sample("Alice", [0.0, 1.0], "pyannote")
    p = repo.upsert_speaker_sample("Alice", [0.0, 0.0, 1.0], "wespeaker-r34")
    assert p.voices == {"pyannote": [0.5, 0.5], "wespeaker-r34": [0.0, 0.0, 1.0]}
    assert p.sample_counts == {"pyannote": 2, "wespeaker-r34": 1} and p.sample_count == 3
    # speakers.embedding is no longer written (an older build reads [] and never matches)
    row = repo._conn.execute("SELECT embedding FROM speakers WHERE name='Alice'").fetchone()
    assert row["embedding"] == "[]"
    assert repo.get_speaker(p.id).voices == p.voices
    assert [x.voices for x in repo.list_speakers()] == [p.voices]
    # a new sample of another length in the same space starts that space over
    p = repo.upsert_speaker_sample("Alice", [1.0, 0.0, 0.0, 0.0], "wespeaker-r34")
    assert (
        p.voices["wespeaker-r34"] == [1.0, 0.0, 0.0, 0.0] and p.sample_counts["wespeaker-r34"] == 1
    )
    assert p.voices["pyannote"] == [0.5, 0.5]


def test_a_vector_never_matches_a_voice_of_another_space(repo):
    repo.upsert_speaker_sample("Alice", A, "wespeaker-r34")  # same length as the query below
    svc = SpeakerService(repo, threshold=0.6)
    assert svc.match({"S": A}) == {}  # pyannote space: Alice has no voice there
    assert svc.match({"S": A}, "wespeaker-r34") == {"S": "Alice"}
    assert svc.match({"S": A_ISH}, "wespeaker-r34", threshold=0.999) == {}
    repo.upsert_speaker_sample("Alice", B, "pyannote")
    assert svc.match({"S": B}) == {"S": "Alice"}
    assert svc.match({"S": B}, "wespeaker-r34") == {}


def test_session_voices_by_space_and_relabel_keeps_every_space(repo):
    from mnemosyne.models.session import Session

    sid = repo.save(Session(name="m")).id
    repo.set_session_voices(sid, "pyannote", {"SPEAKER_00": A, "SPEAKER_01": B})
    repo.set_session_voices(sid, "wespeaker-r34", {"SPEAKER_00": B})
    assert repo.get_session_voices(sid) == {
        "pyannote": {"SPEAKER_00": A, "SPEAKER_01": B},
        "wespeaker-r34": {"SPEAKER_00": B},
    }
    repo.relabel_session_speaker(sid, "SPEAKER_00", "Alice")
    voices = repo.get_session_voices(sid)
    assert set(voices["pyannote"]) == {"Alice", "SPEAKER_01"}
    assert set(voices["wespeaker-r34"]) == {"Alice"}
    # the pyannote-space shorthands read and write that space only
    assert repo.get_session_embeddings(sid) == voices["pyannote"]
    repo.set_session_embeddings(sid, {"Bob": A})
    assert "Bob" in repo.get_session_voices(sid)["pyannote"]
    assert "Bob" not in repo.get_session_voices(sid)["wespeaker-r34"]


def test_rename_enrols_every_space_the_label_has(repo):
    from mnemosyne.models.session import Session

    sid = repo.save(Session(name="m")).id
    repo.set_session_voices(sid, "pyannote", {"SPEAKER_00": A})
    repo.set_session_voices(sid, "wespeaker-r34", {"SPEAKER_00": B, "SPEAKER_01": A})
    SpeakerService(repo).rename(sid, "SPEAKER_00", "Alice")
    (alice,) = repo.list_speakers()
    assert alice.voices == {"pyannote": A, "wespeaker-r34": B} and alice.has_voice


def test_has_voice_is_any_space(client, ctx):
    from mnemosyne.models.session import Session

    sid = ctx.repo.save(Session(name="m", participants=["SPEAKER_00", "SPEAKER_01"])).id
    ctx.repo.set_session_voices(sid, "wespeaker-r34", {"SPEAKER_00": A})
    info = client.get(f"/api/sessions/{sid}/speakers").json()
    assert info == [
        {"label": "SPEAKER_00", "has_voice": True},
        {"label": "SPEAKER_01", "has_voice": False},
    ]


def _before_voice_spaces(path, sid):
    """Turn a database made by this code into one as it was before voice spaces: profiles with
    their vector in `speakers.embedding`, per-meeting voices in `session_speakers`."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP TABLE speaker_voices; DROP TABLE session_voices;
        DELETE FROM meta WHERE key='voice_spaces_migrated';
        CREATE TABLE session_speakers (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            embedding TEXT NOT NULL,
            PRIMARY KEY (session_id, label)
        );
        """
    )
    now = "2026-11-01T10:00:00"
    conn.executemany(
        "INSERT INTO speakers(id, name, embedding, sample_count, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        [
            ("a1", "Alice", "[0.5, 0.5]", 3, now, now),
            ("b1", "Bob", "[]", 1, now, now),  # never had a voice
        ],
    )
    conn.executemany(
        "INSERT INTO session_speakers(session_id, label, embedding) VALUES (?,?,?)",
        [(sid, "SPEAKER_00", "[1.0, 0.0]"), (sid, "SPEAKER_01", "[0.0, 1.0]")],
    )
    conn.commit()
    conn.close()


def test_voices_from_before_spaces_become_pyannote_voices(tmp_path):
    from mnemosyne.models.session import Session

    path = tmp_path / "old.sqlite"
    r = SessionRepository(path)
    sid = r.save(Session(name="m")).id
    r.close()
    _before_voice_spaces(path, sid)

    r = SessionRepository(path)
    try:
        tables = {t[0] for t in r._conn.execute("SELECT name FROM sqlite_master")}
        assert "session_speakers" not in tables and {"speaker_voices", "session_voices"} <= tables
        assert r.get_meta("voice_spaces_migrated") == "1"
        alice = r.get_speaker_by_name("Alice")
        assert alice.voices == {"pyannote": [0.5, 0.5]} and alice.sample_counts == {"pyannote": 3}
        assert (
            r.get_speaker_by_name("Bob").voices == {} and not r.get_speaker_by_name("Bob").has_voice
        )
        assert r.get_session_voices(sid) == {
            "pyannote": {"SPEAKER_00": [1.0, 0.0], "SPEAKER_01": [0.0, 1.0]}
        }
        # the old column is left as it was; it is not read
        row = r._conn.execute("SELECT embedding FROM speakers WHERE id='a1'").fetchone()
        assert row["embedding"] == "[0.5, 0.5]"
        # a profile keeps working: it matches pyannote voices, not another space's
        svc = SpeakerService(r)
        assert svc.match({"S": [0.5, 0.5]}) == {"S": "Alice"}
        assert svc.match({"S": [0.5, 0.5]}, "wespeaker-r34") == {}
    finally:
        r.close()

    # Once: a profile deleted afterwards is not brought back by the next start.
    r = SessionRepository(path)
    try:
        r.delete_speaker("a1")
        r.set_session_voices(sid, "pyannote", {})
    finally:
        r.close()
    r = SessionRepository(path)
    try:
        assert r.get_speaker_by_name("Alice") is None
    finally:
        r.close()


def test_a_fresh_database_has_no_old_table(repo):
    tables = {t[0] for t in repo._conn.execute("SELECT name FROM sqlite_master")}
    assert "session_speakers" not in tables
    assert repo.get_meta("voice_spaces_migrated") == "1"


def test_deleting_a_meeting_or_profile_removes_its_voices(repo):
    from mnemosyne.models.session import Session

    sid = repo.save(Session(name="m")).id
    repo.set_session_voices(sid, "pyannote", {"SPEAKER_00": A})
    p = repo.upsert_speaker_sample("Alice", A)
    repo.delete(sid)
    repo.delete_speaker(p.id)
    for table in ("session_voices", "speaker_voices"):
        assert repo._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.anyio
async def test_composed_engine_reports_the_space_of_its_voices():
    d = FakeDiarizer(embeddings={"SPEAKER_00": A}, space="wespeaker-r34")
    engine = ComposedEngine(FakeTranscriber(), d)
    assert engine.last_speaker_space == ""
    [s async for s in engine.transcribe_sources([AudioSource(path="/m.ogg", kind="mixed")])]
    assert engine.last_speaker_space == "wespeaker-r34"
    # a source without a diarizer's voices says nothing
    plain = ComposedEngine(FakeTranscriber(), None)
    [s async for s in plain.transcribe_sources([AudioSource(path="/m.ogg", kind="mixed")])]
    assert plain.last_speaker_space == ""


def test_auto_naming_matches_within_the_engines_space(client, ctx):
    ctx.repo.upsert_speaker_sample("Alice", A, "pyannote")
    engine = FakeEngine()
    engine.last_speaker_embeddings = {"SPEAKER_00": A}
    engine.last_speaker_space = "wespeaker-r34"  # Alice has no voice there
    sid, _ = _transcribed(client, ctx, engine)
    assert client.get(f"/api/sessions/{sid}").json()["participants"] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert set(ctx.repo.get_session_voices(sid)) == {"wespeaker-r34"}
    # ...and renaming now teaches Alice a voice in that space too
    client.post(
        f"/api/sessions/{sid}/speakers/rename", json={"label": "SPEAKER_00", "name": "Alice"}
    )
    (alice,) = ctx.repo.list_speakers()
    assert set(alice.voices) == {"pyannote", "wespeaker-r34"}
