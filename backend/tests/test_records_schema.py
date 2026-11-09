"""Records v2, task 2: seal and deletion columns, migration, add_seal v2, seal_candidates."""

import sqlite3

import pytest

from mnemosyne.models.session import Session
from mnemosyne.storage import records as r
from mnemosyne.storage.sqlite import SessionRepository


@pytest.fixture
def repo(tmp_path):
    x = SessionRepository(tmp_path / "db.sqlite")
    yield x
    x.close()


def _meeting(repo, name="One") -> Session:
    s = Session(name=name)
    repo.save(s)
    return s


def _manifest(notes="n") -> dict:
    return {"v": 2, "marker": notes}


def test_v1_only_database_opens_keeps_its_seals_and_gains_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    first = SessionRepository(path)
    s = _meeting(first)
    one = first.add_seal(s.id, "created", "h1", {"a.ogg": "x"})
    two = first.add_seal(s.id, "edited", "h2", {"a.ogg": "x"})
    first.log_deletion(s, "audio", "me", "admin", "old")
    first.close()

    # Make it look like a database from before records v2.
    db = sqlite3.connect(path)
    for col in ("format", "manifest", "audio_stat"):
        db.execute(f"ALTER TABLE session_seals DROP COLUMN {col}")
    for col in ("chain_head", "manifest_hash"):
        db.execute(f"ALTER TABLE deletions DROP COLUMN {col}")
    db.commit()
    assert "format" not in {c[1] for c in db.execute("PRAGMA table_info(session_seals)")}
    db.close()

    repo = SessionRepository(path)
    try:
        seals = repo.seals(s.id)
        assert [x["chain"] for x in seals] == [one["chain"], two["chain"]]
        assert all(x["format"] == 1 and x["manifest"] is None for x in seals)
        assert seals[1]["prev"] == one["chain"]
        assert seals[0]["audio"] == {"a.ogg": "x"}
        (d,) = repo.deletions()
        assert d["chain_head"] == "" and d["manifest_hash"] == ""
        # and the v1 path still extends the chain
        three = repo.add_seal(s.id, "again", "h3", {})
        assert three["format"] == 1
        assert repo.seals(s.id)[2]["prev"] == two["chain"]
    finally:
        repo.close()


def test_v1_add_seal_unchanged(repo):
    s = _meeting(repo)
    a = repo.add_seal(s.id, "created", "h", {"x": "1"})
    assert a["chain"] == r.chain_hash("", "h", {"x": "1"}, a["at"], "created")
    assert repo.add_seal(s.id, "again", "h", {"x": "1"}) is None
    assert repo.add_seal(s.id, "again", "h", {"x": "1"}, force=True) is not None


def test_v2_seal_row_and_chain(repo):
    s = _meeting(repo)
    v1 = repo.add_seal(s.id, "created", "h", {})
    m = _manifest()
    v2 = repo.add_seal(s.id, "changed", manifest=m, audio_stat={"a.ogg": [1, 2]})
    assert v2["format"] == 2
    assert v2["content_hash"] == r.manifest_hash(m)
    assert v2["chain"] == r.chain_hash_v2(v1["chain"], r.manifest_hash(m), v2["at"], "changed")
    rows = repo.seals(s.id)
    assert rows[1]["format"] == 2 and rows[1]["audio"] == {}
    assert rows[1]["manifest"] == m
    assert rows[1]["audio_stat"] == {"a.ogg": [1, 2]}
    assert rows[1]["prev"] == v1["chain"]


def test_v2_skip_rule(repo):
    s = _meeting(repo)
    m = _manifest()
    assert repo.add_seal(s.id, "a", manifest=m) is not None
    assert repo.add_seal(s.id, "b", manifest=m) is None
    assert repo.add_seal(s.id, "b", manifest=m, force=True) is not None
    assert repo.add_seal(s.id, "c", manifest=_manifest("other")) is not None
    assert len(repo.seals(s.id)) == 3


def test_v2_is_always_written_after_a_v1_row(repo):
    s = _meeting(repo)
    repo.add_seal(s.id, "created", r.manifest_hash(_manifest()), {})  # same text, but v1
    assert repo.add_seal(s.id, "sealed in the new format", manifest=_manifest()) is not None
    assert [x["format"] for x in repo.seals(s.id)] == [1, 2]


def test_log_deletion_names_the_last_seal(repo):
    s = _meeting(repo)
    t = _meeting(repo, "Two")
    u = _meeting(repo, "Three")
    v1 = repo.add_seal(s.id, "created", "h", {})
    v2 = repo.add_seal(t.id, "created", manifest=_manifest())
    repo.log_deletion(s, "meeting", "me", "admin", "x")
    repo.log_deletion(t, "meeting", "me", "admin", "x")
    repo.log_deletion(u, "meeting", "me", "admin", "x")
    by = {d["session_id"]: d for d in repo.deletions()}
    assert by[s.id]["chain_head"] == v1["chain"] and by[s.id]["manifest_hash"] == ""
    assert by[t.id]["chain_head"] == v2["chain"]
    assert by[t.id]["manifest_hash"] == r.manifest_hash(_manifest())
    assert by[u.id]["chain_head"] == "" and by[u.id]["manifest_hash"] == ""


def test_sync_seq_of(repo):
    s = _meeting(repo)
    seq = repo.sync_seq_of(s.id)
    assert seq is not None and seq > 0
    assert repo.sync_seq_of("nope") is None
    t = _meeting(repo, "Two")
    assert repo.sync_seq_of(t.id) > seq


def test_seal_candidates_follow_changes_since_rules(repo):
    a = _meeting(repo, "a")
    b = _meeting(repo, "b")
    shared = _meeting(repo, "theirs")
    repo._conn.execute("UPDATE sessions SET shared_from='person-1' WHERE id=?", (shared.id,))
    repo._conn.commit()
    got = repo.seal_candidates(0)
    ids = [i for i, _ in got]
    assert ids == [a.id, b.id]
    assert shared.id not in ids
    assert ids == [i for i, _, _ in repo.changes_since(0)[0]]
    assert got[0][1] == repo.sync_seq_of(a.id)
    assert repo.seal_candidates(got[0][1]) == [got[1]]
    assert repo.seal_candidates(0, limit=1) == [got[0]]
    assert repo.seal_candidates(got[1][1]) == []
