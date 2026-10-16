"""Library sync, storage side: schema, the version bump on every write, tombstones, snapshots."""

import json
import re
import sqlite3

import pytest

from mnemosyne.models.session import (
    ActionItem,
    Asset,
    ExternalNotes,
    Recording,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.storage.sqlite import SCHEMA_VERSION, SessionRepository


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "db.sqlite")
    r.device_id = "dev-a"
    yield r
    r.close()


def _meeting(name="One") -> Session:
    return Session(
        name=name,
        status=SessionStatus.COMPLETED,
        transcript=[
            TranscriptSegment(text="hello", speaker="SPEAKER_00", start=0.0, end=1.0),
            TranscriptSegment(text="there", speaker="SPEAKER_01", start=1.0, end=2.0),
        ],
        participants=["SPEAKER_00", "SPEAKER_01"],
        summary_data=SummaryData(action_items=[ActionItem(id="a1", text="do it")]),
    )


def _rec(path="/a/mic.ogg") -> Recording:
    return Recording(source="mic", device_id=1, device_name="Mic", path=path)


@pytest.fixture
def two(repo):
    """Two saved meetings; the first has a bookmark, an event, a flag, an asset and a part."""
    a, b = repo.save(_meeting("A")), repo.save(_meeting("B"))
    repo.add_recordings(a.id, [_rec()])
    repo.add_recordings(b.id, [_rec("/a/b.ogg")])
    return a.id, b.id


def _state(repo, sid):
    seq = repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    _, vv = repo.sync_state(sid)
    return seq, vv.get("dev-a", 0)


def _counter(repo) -> int:
    return repo.current_sync_seq()


# ---- ids ----------------------------------------------------------------


def test_new_meeting_ids_are_12_hex():
    ids = {Session().id for _ in range(50)}
    assert len(ids) == 50
    assert all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids)


# ---- schema and migration -----------------------------------------------


def _downgrade(path):
    """Turn a database made by this code into one as schema 11 left it."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        DROP INDEX sessions_sync_seq; DROP INDEX session_events_uid;
        DROP TABLE sync_served; DROP TABLE sync_tombstones;
        ALTER TABLE sessions DROP COLUMN origin;
        ALTER TABLE sessions DROP COLUMN sync_vv;
        ALTER TABLE sessions DROP COLUMN sync_seq;
        ALTER TABLE session_events DROP COLUMN uid;
        DELETE FROM meta WHERE key='sync_seq';
        UPDATE meta SET value='11' WHERE key='schema_version';
        """
    )
    conn.commit()
    conn.close()


def test_migration_keeps_data_and_adds_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    r = SessionRepository(path)
    first, second = r.save(_meeting("first")), r.save(_meeting("second"))
    r.add_event(first.id, "started")
    r.add_event(first.id, "stopped")
    r.close()
    _downgrade(path)

    r = SessionRepository(path)
    try:
        cols = {c["name"] for c in r._conn.execute("PRAGMA table_info(sessions)")}
        assert {"origin", "sync_vv", "sync_seq"} <= cols
        assert "uid" in {c["name"] for c in r._conn.execute("PRAGMA table_info(session_events)")}
        tables = {t[0] for t in r._conn.execute("SELECT name FROM sqlite_master")}
        assert {"sync_served", "sync_tombstones", "sessions_sync_seq"} <= tables
        assert r.get_meta("schema_version") == str(SCHEMA_VERSION)
        # data intact
        got = r.get(first.id)
        assert got.name == "first" and [s.text for s in got.transcript] == ["hello", "there"]
        assert got.origin == ""
        assert r.sync_state(first.id) == ("", {})
        # existing meetings are listed by the change log, once each, in a stable order
        changes, nxt, more = r.changes_since(0)
        assert {c[0] for c in changes} == {first.id, second.id} and not more
        assert [c[1] for c in changes] == [1, 2] and nxt == 2 and r.current_sync_seq() == 2
        # events got distinct uids
        uids = [x[0] for x in r._conn.execute("SELECT uid FROM session_events")]
        assert len(uids) == 2 and len(set(uids)) == 2 and all(uids)
    finally:
        r.close()
    # opening again changes nothing
    r = SessionRepository(path)
    try:
        assert r.current_sync_seq() == 2
        assert [x[0] for x in r._conn.execute("SELECT uid FROM session_events")] == uids
    finally:
        r.close()


def test_new_events_get_uids(repo, two):
    a, _ = two
    repo.add_event(a, "x")
    repo.add_event(a, "y")
    uids = [
        r[0] for r in repo._conn.execute("SELECT uid FROM session_events WHERE session_id=?", (a,))
    ]
    assert len(set(uids)) == 2 and all(re.fullmatch(r"[0-9a-f]{16}", u) for u in uids)


# ---- the bump -------------------------------------------------------------


def test_save_makes_a_fresh_vector(repo):
    s = repo.save(_meeting())
    seq, n = _state(repo, s.id)
    assert (seq, n) == (1, 1) and repo.sync_state(s.id) == ("", {"dev-a": 1})
    assert repo.get(s.id).updated_at == s.updated_at  # touch keeps save's timestamp
    repo.save(s)
    assert _state(repo, s.id) == (2, 2)


def test_blank_device_id_counts_under_empty_key(tmp_path):
    r = SessionRepository(tmp_path / "x.sqlite")
    try:
        s = r.save(_meeting())
        assert r.sync_state(s.id) == ("", {"": 1}) and r.current_sync_seq() == 1
    finally:
        r.close()


def _item_writer(repo, sid):
    repo.set_action_item_done(sid, 0, True)


def _setup_asset(repo, sid):
    asset = repo.add_asset(Asset(kind="link", title="t", url="http://x"))
    repo.attach_asset(sid, asset.id)
    return asset.id


# name -> (setup(repo, a, b) -> context, action(repo, a, b, ctx), meetings bumped: "a" or "ab")
BUMPERS = {
    "save": (None, lambda r, a, b, c: r.save(r.get(a)), "a"),
    "update_fields_summary": (None, lambda r, a, b, c: r.update_fields(a, summary="s"), "a"),
    "update_fields_name_and_status": (
        None,
        lambda r, a, b, c: r.update_fields(a, name="n", status=SessionStatus.ERROR),
        "a",
    ),
    "replace_segments": (
        None,
        lambda r, a, b, c: r.replace_segments(
            a, [TranscriptSegment(text="x", speaker="S", start=0, end=1)]
        ),
        "a",
    ),
    "edit_segments": (None, lambda r, a, b, c: r.edit_segments(a, lambda segs: segs[:1]), "a"),
    "add_recordings": (
        None,
        lambda r, a, b, c: r.add_recordings(a, [_rec("/a/new.ogg")]),
        "a",
    ),
    "clear_audio": (None, lambda r, a, b, c: r.clear_audio(a), "a"),
    "add_external_notes": (
        None,
        lambda r, a, b, c: r.add_external_notes(a, ExternalNotes(source="Otter", text="t")),
        "a",
    ),
    "delete_external_notes": (
        lambda r, a, b: (r.add_external_notes(a, n := ExternalNotes(source="Otter", text="t")), n)[
            1
        ],
        lambda r, a, b, c: r.delete_external_notes(a, c.id),
        "a",
    ),
    "attach_asset": (
        lambda r, a, b: r.add_asset(Asset(kind="link", title="t", url="http://x")).id,
        lambda r, a, b, c: r.attach_asset(a, c),
        "a",
    ),
    "detach_asset": (
        lambda r, a, b: _setup_asset(r, a),
        lambda r, a, b, c: r.detach_asset(a, c),
        "a",
    ),
    "update_asset": (
        lambda r, a, b: _setup_asset(r, a),
        lambda r, a, b, c: r.update_asset(c, title="renamed"),
        "a",
    ),
    "delete_asset": (
        lambda r, a, b: _setup_asset(r, a),
        lambda r, a, b, c: r.delete_asset(c),
        "a",
    ),
    "add_bookmark": (None, lambda r, a, b, c: r.add_bookmark(a, 0, 1.0, "n"), "a"),
    "update_bookmark": (
        lambda r, a, b: r.add_bookmark(a, 0, 1.0, "n"),
        lambda r, a, b, c: r.update_bookmark(a, c, "m"),
        "a",
    ),
    "delete_bookmark": (
        lambda r, a, b: r.add_bookmark(a, 0, 1.0, "n"),
        lambda r, a, b, c: r.delete_bookmark(a, c),
        "a",
    ),
    "move_bookmarks": (
        lambda r, a, b: r.add_bookmark(a, 0, 1.0, "n"),
        lambda r, a, b, c: r.move_bookmarks([(a, 0, b, 1)]),
        "ab",
    ),
    "move_events": (
        lambda r, a, b: r.add_event(a, "started", part=0),
        lambda r, a, b, c: r.move_events([(a, 0, b, 1)]),
        "ab",
    ),
    "add_live_flag": (None, lambda r, a, b, c: r.add_live_flag(a, "mic", 0, 1, "t"), "a"),
    "delete_live_flag": (
        lambda r, a, b: r.add_live_flag(a, "mic", 0, 1, "t").id,
        lambda r, a, b, c: r.delete_live_flag(a, c),
        "a",
    ),
    "set_action_item_done": (None, lambda r, a, b, c: r.set_action_item_done(a, 0, True), "a"),
    "set_action_item_follow": (None, lambda r, a, b, c: r.set_action_item_follow(a, 0, True), "a"),
    "relabel_session_speaker": (
        None,
        lambda r, a, b, c: r.relabel_session_speaker(a, "SPEAKER_00", "Ann"),
        "a",
    ),
    "set_session_embeddings": (
        None,
        lambda r, a, b, c: r.set_session_embeddings(a, {"SPEAKER_00": [0.1, 0.2]}),
        "a",
    ),
}


@pytest.mark.parametrize("name", BUMPERS)
def test_writer_bumps_once(repo, two, name):
    a, b = two
    setup, act, who = BUMPERS[name]
    ctx = setup(repo, a, b) if setup else None
    before = {s: _state(repo, s) for s in (a, b)}
    counter = _counter(repo)
    act(repo, a, b, ctx)
    after = {s: _state(repo, s) for s in (a, b)}
    touched = [s for s, k in ((a, "a"), (b, "b")) if k in who]
    for s in (a, b):
        if s in touched:
            assert after[s][1] == before[s][1] + 1, name  # vector: exactly one revision
            assert after[s][0] > before[s][0], name
        else:
            assert after[s] == before[s], name
    assert _counter(repo) == counter + len(touched), name
    for s in touched:  # the change log has the newest sequence number
        assert after[s][0] <= _counter(repo)


def test_bump_also_sets_updated_at(repo, two):
    a, _ = two
    for call in (
        lambda: repo.add_external_notes(a, ExternalNotes(source="x", text="t")),
        lambda: repo.add_bookmark(a, 0, 1.0),
        lambda: repo.add_live_flag(a, "mic", 0, 1, "t"),
        lambda: repo.add_recordings(a, [_rec("/a/z.ogg")]),
    ):
        repo._conn.execute("UPDATE sessions SET updated_at='2000-01-01T00:00:00' WHERE id=?", (a,))
        repo._conn.commit()
        call()
        assert repo.get(a).updated_at.year >= 2026


def test_delete_asset_bumps_every_meeting_using_it(repo, two):
    a, b = two
    asset = repo.add_asset(Asset(kind="link", title="t", url="http://x"))
    repo.attach_asset(a, asset.id)
    repo.attach_asset(b, asset.id)
    before = {s: _state(repo, s)[1] for s in (a, b)}
    repo.update_asset(asset.id, title="new")
    mid = {s: _state(repo, s)[1] for s in (a, b)}
    repo.delete_asset(asset.id)
    after = {s: _state(repo, s)[1] for s in (a, b)}
    assert all(mid[s] == before[s] + 1 and after[s] == mid[s] + 1 for s in (a, b))


def test_no_ops_do_not_bump(repo, two):
    a, _ = two
    before = _state(repo, a)
    assert repo.delete_bookmark(a, "nope") is False
    assert repo.delete_live_flag(a, "nope") is False
    assert repo.delete_external_notes(a, "nope") is False
    assert repo.detach_asset(a, "nope") is False
    assert repo.update_bookmark(a, "nope", "x") is False
    repo.add_recordings(a, [])
    repo.set_session_embeddings(a, {})
    assert repo.set_action_item_done(a, 9, True) is False
    assert _state(repo, a) == before


def test_status_and_audio_file_alone_do_not_bump(repo, two):
    a, _ = two
    before = _state(repo, a)
    repo.update_fields(a, status=SessionStatus.ERROR)
    repo.update_fields(a, audio_file="/x.ogg")
    repo.update_fields(a, status=SessionStatus.COMPLETED, audio_file="/y.ogg")
    assert _state(repo, a) == before
    assert repo.get(a).audio_file == "/y.ogg"
    assert repo.update_fields("missing", name="x") is None


def test_failed_write_leaves_no_trace(repo, two):
    a, _ = two
    before, counter = _state(repo, a), _counter(repo)
    with pytest.raises(ValueError):
        repo.update_fields(a, nonsense=1)
    assert _state(repo, a) == before and _counter(repo) == counter


def test_quiet_set_audio_does_not_bump(repo, two, tmp_path):
    a, _ = two
    from mnemosyne.services.session_service import SessionService

    svc = SessionService(repo, tmp_path / "rec")
    before = _state(repo, a)
    svc.set_audio(a, "/q/mixed.ogg", [_rec("/q/mic.ogg")], quiet=True)
    assert _state(repo, a) == before
    assert repo.get(a).audio_file == "/q/mixed.ogg"
    assert "/q/mic.ogg" in [r.path for r in repo.get(a).recordings]
    svc.set_audio(a, "/q/mixed.ogg", [_rec("/q/mic2.ogg")])  # not quiet: a new part
    assert _state(repo, a)[1] == before[1] + 1


def test_rebase_paths_does_not_bump_and_spares_other_machines(repo, two):
    a, b = two
    repo.update_fields(a, audio_file="/old/rec/a.ogg")
    repo.add_recordings(a, [_rec("/old/rec/mic.ogg")])
    repo.add_recordings(b, [_rec("/old/rec/b.ogg")])
    repo.set_sync_state(b, {"dev-b": 3}, "dev-b")  # recorded elsewhere
    before = _state(repo, a)
    repo.rebase_paths("/old/rec", "/new/rec")
    assert _state(repo, a) == before
    got = repo.get(a)
    assert got.audio_file == "/new/rec/a.ogg"
    assert "/new/rec/mic.ogg" in [r.path for r in got.recordings]
    assert "/old/rec/b.ogg" in [r.path for r in repo.get(b).recordings]
    assert not any(r.path.startswith("/new/rec") for r in repo.get(b).recordings)


NEVER = {
    "add_event": lambda r, a: r.add_event(a, "viewed"),
    "put_vault_note": lambda r, a: r.put_vault_note(a, "p.md", b"h", {}, "sha"),
    "put_summary_doc": lambda r, a: r.put_summary_doc(a, {"x": 1}),
    "keep_version": lambda r, a: r.keep_version(r.get(a), "edited"),
    "set_meta": lambda r, a: r.set_meta("k", "v"),
    "set_attendee_emails": lambda r, a: r.set_attendee_emails(a, {"A": "a@x"}),
    "set_legal_hold": lambda r, a: r.set_legal_hold(a, "why"),
    "set_crm_state": lambda r, a: r.set_crm_state(a, "hubspot", {"id": 1}),
    "add_review": lambda r, a: r.add_review(a, "ok"),
    "replace_vectors": lambda r, a: r.replace_vectors(a, []),
    "set_shares": lambda r, a: r.set_shares(a, {"u1"}, "me"),
    "adopt_unowned": lambda r, a: r.adopt_unowned("u1"),
    "set_sync_state": lambda r, a: r.set_sync_state(a, {"dev-b": 1}, "dev-b"),
    "serve_snapshot": lambda r, a: r.serve_snapshot(a, "p", 1, "{}"),
}


@pytest.mark.parametrize("name", NEVER)
def test_local_bookkeeping_does_not_bump(repo, two, name):
    a, _ = two
    counter = _counter(repo)
    seq = repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (a,)).fetchone()[0]
    vv_before = repo.sync_state(a)[1]
    NEVER[name](repo, a)
    assert _counter(repo) == counter
    assert repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (a,)).fetchone()[0] == seq
    if name != "set_sync_state":
        assert repo.sync_state(a)[1] == vv_before


# ---- delete and tombstones -------------------------------------------------


def test_delete_writes_a_tombstone_that_outlives_it(repo, two):
    a, b = two
    repo.serve_snapshot(a, "peer", 1, "{}")
    repo.add_event(a, "x")
    assert repo.delete(a, device_id="dev-z") is True
    assert repo.get(a) is None
    assert repo.has_tombstone(a) and not repo.has_tombstone(b)
    (t,) = repo.tombstones_since("")
    assert (t["session_id"], t["device"], t["name"]) == (a, "dev-z", "A")
    assert repo.served_snapshot(a, "peer", 1) is None  # served states go with the meeting
    assert repo._conn.execute("SELECT count(*) FROM session_events").fetchone()[0] == 0
    assert repo.tombstones_since(t["at"]) == []  # strictly after
    assert repo.tombstones_since("2000-01-01") == [t]


def test_delete_defaults_to_this_device_and_can_skip_the_tombstone(repo, two):
    a, b = two
    repo.delete(a)
    assert repo.tombstones_since("")[0]["device"] == "dev-a"
    repo.delete(b, tombstone=False)
    assert not repo.has_tombstone(b) and repo.get(b) is None
    assert repo.delete("missing") is False and not repo.has_tombstone("missing")


def test_tombstone_survives_reopen(repo, tmp_path, two):
    a, _ = two
    repo.delete(a)
    repo.close()
    r = SessionRepository(tmp_path / "db.sqlite")
    try:
        assert r.has_tombstone(a)
    finally:
        r.close()
    repo.reopen(tmp_path / "db.sqlite")


# ---- change log, state, snapshots -------------------------------------------


def test_changes_since_pages_in_order(repo):
    ids = [repo.save(_meeting(str(i))).id for i in range(5)]
    repo.add_bookmark(ids[0], 0, 1.0)  # ids[0] moves to the head of the log
    changes, nxt, more = repo.changes_since(0, 3)
    assert [c[0] for c in changes] == ids[1:4] and more and nxt == changes[-1][1]
    changes2, nxt2, more2 = repo.changes_since(nxt, 3)
    assert [c[0] for c in changes2] == [ids[4], ids[0]] and not more2
    assert nxt2 == repo.current_sync_seq()
    assert repo.changes_since(nxt2) == ([], nxt2, False)
    # exactly a page: no "more"
    _, _, more3 = repo.changes_since(0, 5)
    assert more3 is False
    assert changes2[1][2] == {"dev-a": 2}  # the vector comes along as a dict


def test_sync_state_roundtrip(repo, two):
    a, _ = two
    repo.set_sync_state(a, {"dev-b": 4, "dev-a": 2}, "dev-b")
    assert repo.sync_state(a) == ("dev-b", {"dev-a": 2, "dev-b": 4})
    assert repo.get(a).origin == "dev-b"
    assert [s.origin for s in repo.list_summaries() if s.id == a] == ["dev-b"]
    assert repo.sync_state("missing") is None
    repo.save(repo.get(a))  # save keeps the origin
    assert repo.get(a).origin == "dev-b"
    assert repo.sync_state(a)[1] == {"dev-a": 3, "dev-b": 4}


def test_snapshots_keep_the_last_three_revisions_per_peer(repo, two):
    a, b = two
    for rev in range(1, 7):
        repo.serve_snapshot(a, "p1", rev, json.dumps({"rev": rev}))
    repo.serve_snapshot(a, "p2", 1, "other")
    repo.serve_snapshot(b, "p1", 1, "b")
    assert [repo.served_snapshot(a, "p1", r) is not None for r in range(1, 7)] == [
        False,
        False,
        False,
        True,
        True,
        True,
    ]
    assert json.loads(repo.served_snapshot(a, "p1", 6)) == {"rev": 6}
    assert repo.served_snapshot(a, "p2", 1) == "other" and repo.served_snapshot(b, "p1", 1) == "b"
    repo.serve_snapshot(a, "p1", 5, "again")  # replaced in place, nothing else dropped
    assert repo.served_snapshot(a, "p1", 5) == "again" and repo.served_snapshot(a, "p1", 4)
    assert repo.served_snapshot(a, "p1", 99) is None
