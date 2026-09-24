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
    assert p.sample_count == 2 and p.embedding == [0.5, 0.5]
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
