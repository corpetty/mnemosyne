"""SQLite repository and legacy JSON import."""

import json
from datetime import datetime

import pytest
from src.mnemosyne.models.session import Recording, Session, SessionStatus
from src.mnemosyne.models.transcript import TranscriptSegment, WordSegment
from src.mnemosyne.storage.sqlite import SessionRepository, import_json_sessions


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "db.sqlite")
    yield r
    r.close()


def _session():
    return Session(
        name="Full",
        status=SessionStatus.COMPLETED,
        audio_file="/a/mixed.ogg",
        recordings=[
            Recording(source="mic", device_id=1, device_name="Mic", path="/a/mic.ogg"),
            Recording(source="system", device_id=2, device_name="Speakers", path="/a/sys.ogg"),
        ],
        transcript=[
            TranscriptSegment(
                text="hi",
                speaker="SPEAKER_00",
                start=0.0,
                end=0.5,
                words=[WordSegment(word="hi", start=0.0, end=0.5, score=0.9)],
            ),
            TranscriptSegment(text="yo", speaker="SPEAKER_01", start=0.6, end=1.0),
        ],
        summary="sum",
        notes="n",
        participants=["SPEAKER_00", "SPEAKER_01"],
    )


def test_roundtrip(repo):
    s = _session()
    repo.save(s)
    loaded = repo.get(s.id)
    assert loaded == s
    assert loaded.transcript[0].words[0].word == "hi"
    assert loaded.transcript[1].words is None
    assert [r.source for r in loaded.recordings] == ["mic", "system"]


def test_list_summaries_does_not_need_transcript(repo):
    s = _session()
    repo.save(s)
    repo.save(Session(name="Empty"))
    summaries = {x.id: x for x in repo.list_summaries()}
    assert summaries[s.id].has_transcript is True
    assert summaries[s.id].has_summary is True
    assert summaries[s.id].participant_count == 2
    empty = next(x for x in summaries.values() if x.name == "Empty")
    assert empty.has_transcript is False


def test_update_fields_and_replace_segments(repo):
    s = repo.save(Session(name="x"))
    updated = repo.update_fields(s.id, name="y", status=SessionStatus.ERROR)
    assert updated.name == "y" and updated.status == SessionStatus.ERROR
    assert updated.updated_at >= s.updated_at

    repo.replace_segments(s.id, [TranscriptSegment(text="t", speaker="S", start=0, end=1)])
    assert len(repo.get(s.id).transcript) == 1
    repo.replace_segments(s.id, [])
    assert repo.get(s.id).transcript == []

    with pytest.raises(ValueError):
        repo.update_fields(s.id, id="nope")
    assert repo.update_fields("missing", name="z") is None


def test_delete_cascades(repo):
    s = _session()
    repo.save(s)
    assert repo.delete(s.id) is True
    assert repo.get(s.id) is None
    assert repo.delete(s.id) is False
    with repo._lock:
        assert repo._conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
        assert repo._conn.execute("SELECT count(*) FROM recordings").fetchone()[0] == 0


def test_import_legacy_json(repo, tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    legacy = {
        "id": "abc12345",
        "name": "Old",
        "status": "processing",
        "created_at": "2026-02-18T15:29:00.979972",
        "updated_at": "2026-02-18T15:29:00.980046",
        "audio_file": None,
        "transcript": [{"text": "hey", "speaker": "SPEAKER_00", "start": 0.0, "end": 1.0}],
        "summary": "",
        "notes": "",
        "participants": ["SPEAKER_00"],
    }
    (sessions_dir / "abc12345.json").write_text(json.dumps(legacy))
    (sessions_dir / "broken.json").write_text("{not json")

    assert import_json_sessions(repo, sessions_dir) == 1
    assert import_json_sessions(repo, sessions_dir) == 0  # idempotent

    s = repo.get("abc12345")
    assert s.name == "Old"
    assert s.status == SessionStatus.COMPLETED  # processing + transcript -> completed
    assert s.created_at == datetime(2026, 2, 18, 15, 29, 0, 979972)
    assert (sessions_dir / "abc12345.json").exists()  # left in place


def test_app_imports_legacy_sessions_on_start(settings):
    from fastapi.testclient import TestClient
    from src.mnemosyne.api.app import create_app

    settings.sessions_dir.mkdir(parents=True)
    (settings.sessions_dir / "z.json").write_text(
        json.dumps({"id": "zzzzzzzz", "name": "Legacy", "status": "created"})
    )
    with TestClient(create_app(settings)) as client:
        names = [s["name"] for s in client.get("/api/sessions").json()]
    assert names == ["Legacy"]
