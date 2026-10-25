"""Guest notes rooms: storage, settings and the API model (phase 9)."""

import pytest
from pydantic import ValidationError

from mnemosyne.config import Settings
from mnemosyne.models.guest_notes import GuestRoom
from mnemosyne.models.session import Session
from mnemosyne.storage.sqlite import SessionRepository

KEY = bytes(range(32))


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "db.sqlite")
    yield r
    r.close()


def _meeting(repo, name="One") -> str:
    s = Session(name=name)
    repo.save(s)
    return s.id


def _row(sid, **kw) -> dict:
    row = {
        "session_id": sid,
        "room": "room-1",
        "key": KEY,
        "token": "pub-token",
        "room_token": "room-token",
        "host_url": "https://pub.example.org",
        "created_at": "2026-10-24T10:00:00+00:00",
    }
    return {**row, **kw}


def test_round_trip_defaults_and_key_bytes(repo):
    sid = _meeting(repo)
    assert repo.guest_room(sid) is None and repo.live_guest_rooms() == []
    repo.put_guest_room(_row(sid))
    got = repo.guest_room(sid)
    assert got["key"] == KEY and isinstance(got["key"], bytes)
    assert (got["ended_at"], got["deleted_at"], got["expires_at"], got["auto_end_at"]) == ("",) * 4
    assert got["started_by"] == "" and got["last_seq"] == 0
    assert got["room"] == "room-1" and got["token"] == "pub-token"
    # Replace, not a second row.
    repo.put_guest_room(_row(sid, room="room-2", started_by="u1", last_seq=4))
    assert repo.guest_room(sid)["room"] == "room-2"
    assert len(repo.live_guest_rooms()) == 1


def test_set_and_live(repo):
    a, b = _meeting(repo, "A"), _meeting(repo, "B")
    repo.put_guest_room(_row(a))
    repo.put_guest_room(_row(b, created_at="2026-10-24T11:00:00+00:00"))
    repo.set_guest_room(a, ended_at="2026-10-24T12:00:00+00:00", last_seq=9)
    live = repo.live_guest_rooms()
    assert [r["session_id"] for r in live] == [a, b]  # ended ones stay
    assert live[0]["ended_at"] and live[0]["last_seq"] == 9
    repo.set_guest_room(b, deleted_at="2026-10-24T13:00:00+00:00")
    assert [r["session_id"] for r in repo.live_guest_rooms()] == [a]
    assert repo.guest_room(b)["deleted_at"]  # still readable
    with pytest.raises(ValueError):
        repo.set_guest_room(a, nope=1)


def test_room_goes_with_the_meeting_and_is_no_new_version(repo):
    sid = _meeting(repo)
    before = repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    repo.put_guest_room(_row(sid))
    repo.set_guest_room(sid, last_seq=1)
    after = repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    assert before == after  # does not travel in library sync
    repo.delete(sid)
    assert repo.guest_room(sid) is None and repo.live_guest_rooms() == []


def test_settings_defaults_and_validator():
    s = Settings()
    assert s.guest_host == "" and s.allow_guest_notes is False
    assert s.guest_notes_end_hours == 12
    assert Settings(guest_notes_end_hours=1).guest_notes_end_hours == 1
    with pytest.raises(ValidationError):
        Settings(guest_notes_end_hours=0)


def test_model_defaults():
    g = GuestRoom(url="u", host_url="h", state="live", created_at="t")
    assert g.guests_present == [] and g.ended_at == "" and g.started_by == ""
    with pytest.raises(ValidationError):
        GuestRoom(url="u", host_url="h", state="gone", created_at="t")
    required = GuestRoom.model_json_schema(mode="serialization")["required"]
    assert "guests_present" in required and "expires_at" in required
