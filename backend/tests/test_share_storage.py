"""Sharing meetings with contacts, storage side (phase 8, task 9): `peer_shares`, the
`share_grants` view behind `_SEES`, `sessions.shared_from`, signatures, the change feed and
tombstones a contact pulls, and peer shares travelling between one person's computers."""

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from mnemosyne import access
from mnemosyne.models.session import (
    ActionItem,
    Session,
    SessionStatus,
    SummaryData,
    TranscriptSegment,
)
from mnemosyne.models.sync_session import SyncPeerShare, merge_peer_shares
from mnemosyne.services import decision_links, origin, sync_state, task_links
from mnemosyne.storage.sqlite import SCHEMA_VERSION, SessionRepository
from tests.test_publishing import _meeting as _pub_meeting
from tests.test_publishing import _publish, seals  # noqa: F401
from tests.test_sync_service import meeting, nodes, pull, sidecar  # noqa: F401

pytestmark = pytest.mark.anyio

ADA = "a" * 64
BOB = "b" * 64
PARTS = {"summary": True, "quotes": [1, 2]}


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "db.sqlite")
    r.device_id = "dev-a"
    yield r
    r.close()


def _save(repo, name="One", **kw) -> str:
    fields = dict(
        name=name,
        status=SessionStatus.COMPLETED,
        transcript=[TranscriptSegment(text="hello", speaker="Ann", start=0.0, end=1.0)],
    )
    fields.update(kw)
    return repo.save(Session(**fields)).id


def _contact(repo, person=ADA, name="Ada", my_name=""):
    repo.put_contact(
        {
            "person": person,
            "name": name,
            "my_name": my_name,
            "card": {"name": name, "machines": [], "at": "2026-10-23T10:00:00+00:00"},
            "secret": "s" * 64,
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
        }
    )


def _shared_from(repo, sid, person=ADA):
    with repo._lock, repo._conn:
        repo._conn.execute("UPDATE sessions SET shared_from=? WHERE id=?", (person, sid))


def _seq(repo, sid) -> int:
    return repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]


def _as(person=ADA, name="Ada", role=access.MEMBER):
    return access.current.set(access.Principal("p:" + person[:16], name, role))


# ---- schema ------------------------------------------------------------------------------------


def test_schema_version_and_a_v13_database_gains_the_new_parts(tmp_path):
    assert SCHEMA_VERSION == 16
    path = tmp_path / "db.sqlite"
    r = SessionRepository(path)
    r.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP VIEW share_grants")
    raw.execute("DROP TABLE peer_shares")
    raw.execute("DROP TABLE signatures")
    raw.execute("ALTER TABLE sessions DROP COLUMN shared_from")
    raw.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
    raw.commit()
    raw.close()
    r = SessionRepository(path)
    sid = _save(r)
    r.put_peer_share(sid, ADA, PARTS, "me")
    assert r.add_signature("x", ADA, "sig")
    assert r.get(sid).shared_from == ""
    assert r.get_meta("schema_version") == "16"
    r.close()


def test_the_view_is_recreated_on_every_open(tmp_path):
    path = tmp_path / "db.sqlite"
    SessionRepository(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DROP VIEW share_grants")
    raw.execute("CREATE VIEW share_grants(session_id, user_id) AS SELECT 'x', 'y'")
    raw.commit()
    raw.close()
    r = SessionRepository(path)
    sid = _save(r)
    r.put_peer_share(sid, ADA, PARTS, "me")
    rows = r._conn.execute("SELECT session_id, user_id FROM share_grants").fetchall()
    assert [tuple(x) for x in rows] == [(sid, "p:" + ADA[:16])]
    r.close()


# ---- who sees what -----------------------------------------------------------------------------


def test_a_contact_sees_exactly_the_meetings_shared_with_them(repo):
    shared, other, bobs = _save(repo, "Shared"), _save(repo, "Other"), _save(repo, "Bobs")
    repo.put_peer_share(shared, ADA, PARTS, "me")
    repo.put_peer_share(bobs, BOB, PARTS, "me")
    reset = _as()
    try:
        assert [s.id for s in repo.list_summaries()] == [shared]
        assert repo.visible_ids() == {shared}
        assert repo.get(shared).name == "Shared"
        assert repo.get(other) is None and repo.get(bobs) is None
        assert repo.exists(shared) and not repo.exists(other)
        with pytest.raises(access.Hidden):
            repo._check_write(other)
        # Visible, but not theirs to change.
        with pytest.raises(access.Forbidden) as err:
            repo._check_write(shared)
        assert not isinstance(err.value, access.Hidden)
    finally:
        access.current.reset(reset)
    # The desktop (no principal) sees all three.
    assert len(repo.list_summaries()) == 3


def test_revoking_hides_the_meeting_and_sharing_again_shows_it(repo):
    sid = _save(repo)
    repo.put_peer_share(sid, ADA, PARTS, "me")
    assert repo.revoke_peer_share(sid, ADA)
    reset = _as()
    try:
        assert repo.get(sid) is None and repo.visible_ids() == set()
        with pytest.raises(access.Hidden):
            repo._check_write(sid)
    finally:
        access.current.reset(reset)
    repo.put_peer_share(sid, ADA, PARTS, "me")
    reset = _as()
    try:
        assert repo.get(sid) is not None
    finally:
        access.current.reset(reset)


def test_team_shares_still_work_through_the_view(repo):
    sid = _save(repo, owner_id="ann")
    repo.set_shares(sid, {"bob"}, "ann")
    reset = access.current.set(access.Principal("bob", "Bob", access.MEMBER))
    try:
        assert repo.get(sid) is not None and repo.visible_ids() == {sid}
    finally:
        access.current.reset(reset)
    reset = access.current.set(access.Principal("cat", "Cat", access.MEMBER))
    try:
        assert repo.get(sid) is None
    finally:
        access.current.reset(reset)
    repo.set_shares(sid, {"*"}, "ann")
    reset = access.current.set(access.Principal("cat", "Cat", access.MEMBER))
    try:
        assert repo.get(sid) is not None
    finally:
        access.current.reset(reset)


# ---- peer share rows ---------------------------------------------------------------------------


def test_put_and_revoke_keep_one_row_and_touch_the_meeting(repo):
    sid = _save(repo)
    seq0 = _seq(repo, sid)
    row = repo.put_peer_share(sid, ADA, PARTS, "me")
    assert row["parts"] == PARTS and row["by"] == "me" and row["revoked_at"] == ""
    seq1 = _seq(repo, sid)
    assert seq1 > seq0 and repo.sync_state(sid)[1]["dev-a"] >= 2

    changed = repo.put_peer_share(sid, ADA, {"summary": False}, "me2")
    assert changed["parts"] == {"summary": False} and changed["at"] == row["at"]
    assert changed["updated_at"] >= row["updated_at"]
    seq2 = _seq(repo, sid)
    assert seq2 > seq1 and len(repo.peer_shares(sid)) == 1

    assert repo.revoke_peer_share(sid, ADA)
    seq3 = _seq(repo, sid)
    assert seq3 > seq2
    assert repo.peer_shares(sid) == []
    (gone,) = repo.peer_shares(sid, include_revoked=True)
    assert gone["revoked_at"] and gone["updated_at"] == gone["revoked_at"]
    assert repo.peer_share(sid, ADA)["revoked_at"] == gone["revoked_at"]
    assert not repo.revoke_peer_share(sid, ADA)  # already revoked: nothing changes
    assert _seq(repo, sid) == seq3

    again = repo.put_peer_share(sid, ADA, PARTS, "me")  # shared again: a fresh share
    assert again["revoked_at"] == "" and again["at"] > row["at"]
    assert repo.peer_share(sid, BOB) is None


def test_putting_a_share_for_an_unknown_meeting_is_an_error(repo):
    with pytest.raises(KeyError):
        repo.put_peer_share("nope", ADA, PARTS, "me")


def test_shares_for_person_lists_only_live_ones(repo):
    a, b, c = _save(repo, "A"), _save(repo, "B"), _save(repo, "C")
    repo.put_peer_share(a, ADA, PARTS, "me")
    repo.put_peer_share(b, ADA, PARTS, "me")
    repo.put_peer_share(c, BOB, PARTS, "me")
    repo.revoke_peer_share(b, ADA)
    assert [s["session_id"] for s in repo.shares_for_person(ADA)] == [a]
    assert [s["session_id"] for s in repo.shares_for_person(BOB)] == [c]


def test_deleting_a_meeting_removes_its_shares(repo):
    sid = _save(repo)
    repo.put_peer_share(sid, ADA, PARTS, "me")
    repo.delete(sid)
    assert repo.peer_shares(sid, include_revoked=True) == []


# ---- what a contact pulls ----------------------------------------------------------------------


def test_shared_changes_since_lists_live_shared_meetings_in_change_order(repo):
    a, b, c, d = (_save(repo, n) for n in "ABCD")
    repo.put_peer_share(a, ADA, PARTS, "me")
    repo.put_peer_share(b, ADA, PARTS, "me")
    repo.put_peer_share(d, BOB, PARTS, "me")
    changes, nxt, more = repo.shared_changes_since(ADA, 0)
    assert [sid for sid, _ in changes] == [a, b] and not more
    assert nxt == changes[-1][1] and changes[0][1] < changes[1][1]
    assert repo.shared_changes_since(ADA, nxt) == ([], nxt, False)
    repo.update_fields(a, name="Renamed")
    changes2, nxt2, _ = repo.shared_changes_since(ADA, nxt)
    assert [sid for sid, _ in changes2] == [a] and nxt2 > nxt
    first, nxt3, more = repo.shared_changes_since(ADA, 0, limit=1)
    assert len(first) == 1 and more and nxt3 == first[0][1]
    repo.revoke_peer_share(a, ADA)  # no longer listed
    assert [sid for sid, _ in repo.shared_changes_since(ADA, 0)[0]] == [b]
    assert c not in [sid for sid, _ in repo.shared_changes_since(ADA, 0)[0]]


def test_shared_tombstones_cover_unshared_and_deleted_meetings(repo):
    a, b, c = _save(repo, "A"), _save(repo, "B"), _save(repo, "C")
    for sid in (a, b, c):
        repo.put_peer_share(sid, ADA, PARTS, "me")
    repo.put_peer_share(b, BOB, PARTS, "me")
    before = datetime.now().isoformat()
    repo.revoke_peer_share(a, ADA)
    repo.delete(b)
    stones = repo.shared_tombstones_since(ADA, before)
    assert [(t["session_id"], t["reason"]) for t in stones] == [(a, "unshared"), (b, "deleted")]
    assert all(t["at"] > before for t in stones)
    # Bob had a share of B only.
    assert [(t["session_id"], t["reason"]) for t in repo.shared_tombstones_since(BOB)] == [
        (b, "deleted")
    ]
    # Asked from after the last one: nothing.
    assert repo.shared_tombstones_since(ADA, stones[-1]["at"]) == []
    # A share that was revoked before the deletion is still reported as deleted afterwards.
    repo.delete(a)
    reasons = {(t["session_id"], t["reason"]) for t in repo.shared_tombstones_since(ADA)}
    assert (a, "deleted") in reasons and (b, "deleted") in reasons
    # Shared again after an unshare: no longer "unshared".
    repo.put_peer_share(c, ADA, PARTS, "me")
    repo.revoke_peer_share(c, ADA)
    repo.put_peer_share(c, ADA, PARTS, "me")
    assert c not in {t["session_id"] for t in repo.shared_tombstones_since(ADA)}


def test_a_peer_driven_delete_still_tells_the_contact(repo):
    sid = _save(repo)
    repo.put_peer_share(sid, ADA, PARTS, "me")
    repo.delete(sid, tombstone=False)
    assert [t["reason"] for t in repo.shared_tombstones_since(ADA)] == ["deleted"]
    assert not repo.has_tombstone(sid)


# ---- shared_from -------------------------------------------------------------------------------


def test_shared_from_is_read_into_session_and_summary(repo):
    sid = _save(repo)
    assert repo.get(sid).shared_from == ""
    _shared_from(repo, sid)
    assert repo.get(sid).shared_from == ADA
    assert repo.list_summaries()[0].shared_from == ADA


def test_changes_since_omits_shared_from_rows(repo):
    mine, theirs = _save(repo, "Mine"), _save(repo, "Theirs")
    _shared_from(repo, theirs)
    changes, _, _ = repo.changes_since(0)
    assert [sid for sid, _, _ in changes] == [mine]


def test_writes_to_a_shared_meeting_are_refused_with_the_sharers_name(repo):
    sid = _save(repo)
    _shared_from(repo, sid)
    _contact(repo, my_name="Ada L.")
    with pytest.raises(access.Forbidden) as err:
        repo._check_write(sid)
    assert str(err.value) == (
        "Shared by Ada L.: you can comment, tick action items and type in the notes"
    )
    with pytest.raises(access.Forbidden):
        repo.update_fields(sid, name="x")
    with pytest.raises(access.Forbidden):
        repo.delete(sid)
    assert repo.get(sid).name == "One"
    with access.internally():
        repo._check_write(sid)  # the app's own writer
        repo.update_fields(sid, name="Renamed")
    assert repo.get(sid).name == "Renamed"


def test_the_sharers_name_falls_back_to_their_name_then_to_someone(repo):
    sid = _save(repo)
    _shared_from(repo, sid)
    with pytest.raises(access.Forbidden, match="Shared by someone:"):
        repo._check_write(sid)
    _contact(repo, name="Ada")
    with pytest.raises(access.Forbidden, match="Shared by Ada:"):
        repo._check_write(sid)


def test_deleting_a_shared_from_row_leaves_no_sync_tombstone(repo):
    mine, theirs = _save(repo, "Mine"), _save(repo, "Theirs")
    _shared_from(repo, theirs)
    with access.internally():
        assert repo.delete(theirs)
    assert repo.get(theirs) is None and not repo.has_tombstone(theirs)
    assert repo.tombstones_since() == []
    assert repo.delete(mine)
    assert repo.has_tombstone(mine)


def test_task_linking_ignores_shared_from_meetings(repo):
    data = SummaryData(action_items=[ActionItem(id="a1", text="Send the plan")])
    mine = _save(repo, "Mine", summary_data=data)
    theirs = _save(repo, "Theirs", summary_data=data)
    _shared_from(repo, theirs)
    assert [m.id for m in task_links._meetings(repo)] == [mine]


# ---- signatures --------------------------------------------------------------------------------


def test_a_signature_is_stored_once(repo):
    assert not repo.has_signature("rec1")
    assert repo.add_signature("rec1", ADA, "sig1")
    assert repo.has_signature("rec1")
    assert not repo.add_signature("rec1", BOB, "sig2")  # idempotent: the first stands
    row = repo._conn.execute("SELECT signer, sig FROM signatures WHERE subject='rec1'").fetchone()
    assert (row["signer"], row["sig"]) == (ADA, "sig1")
    assert not repo.has_signature("rec2")


# ---- origin helpers ----------------------------------------------------------------------------


def test_a_shared_meeting_is_foreign_and_the_audio_is_with_the_sharer(repo, tmp_path):
    app = SimpleNamespace(repo=repo, settings=SimpleNamespace(data_dir=tmp_path))
    sid = _save(repo)
    assert not origin.is_foreign(repo.get(sid))
    _shared_from(repo, sid)
    _contact(repo, my_name="Ada L.")
    s = repo.get(sid)
    assert origin.is_foreign(s)
    assert origin.origin_name(app, s) == "Ada L."
    assert origin.audio_elsewhere(app, s) == "The audio is with Ada L."
    with pytest.raises(Exception, match="The audio is with Ada L.") as err:
        origin.require_local_audio(app, s)
    assert err.value.status_code == 409
    assert origin.skip_foreign(s, "x")
    # Recorded on another of my computers: unchanged.
    other = repo.get(_save(repo, "Elsewhere"))
    other.origin = "dev-b"
    assert origin.is_foreign(other)
    assert origin.audio_elsewhere(app, other) == "The audio is on another computer"


# ---- library sync ------------------------------------------------------------------------------


def test_merge_peer_shares_newest_wins_and_a_tie_is_revoked():
    t = datetime(2026, 10, 23, 10, 0, 0)

    def share(person, minutes, revoked=False, parts=None):
        at = (t + timedelta(minutes=minutes)).isoformat()
        return SyncPeerShare(
            person=person,
            parts=parts or {},
            by="me",
            at=t.isoformat(),
            updated_at=at,
            revoked_at=at if revoked else "",
        )

    ours = [share(ADA, 1, parts={"x": 1}), share(BOB, 5)]
    theirs = [share(ADA, 2, revoked=True), share(BOB, 3), share("c" * 64, 1)]
    merged = {p.person: p for p in merge_peer_shares(ours, theirs)}
    assert merged[ADA].revoked_at and merged[BOB].updated_at == ours[1].updated_at
    assert set(merged) == {ADA, BOB, "c" * 64}
    tie = merge_peer_shares([share(ADA, 4)], [share(ADA, 4, revoked=True)])
    assert tie[0].revoked_at
    assert merge_peer_shares([share(ADA, 4, revoked=True)], [share(ADA, 4)])[0].revoked_at
    # An older peer that omits the field loses nothing.
    assert merge_peer_shares(ours, []) == ours and merge_peer_shares([], ours) == ours


async def test_peer_shares_travel_between_my_computers(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    a.repo.put_peer_share(sid, ADA, PARTS, "me")
    await pull(b, a)
    (got,) = b.repo.peer_shares(sid)
    assert got["person"] == ADA and got["parts"] == PARTS
    assert got["updated_at"] == a.repo.peer_share(sid, ADA)["updated_at"]

    # B revokes; the revocation comes back to A.
    b.repo.revoke_peer_share(sid, ADA)
    await pull(a, b)
    assert a.repo.peer_shares(sid) == []
    assert a.repo.peer_share(sid, ADA)["revoked_at"]
    assert b.repo.peer_share(sid, ADA)["revoked_at"] == a.repo.peer_share(sid, ADA)["revoked_at"]


async def test_concurrent_share_changes_merge_by_person(nodes, sidecar):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    a.repo.put_peer_share(sid, ADA, PARTS, "me")
    await pull(b, a)
    # Concurrently: A shares with Bob; B revokes Ada (later than anything A did to Ada).
    a.repo.put_peer_share(sid, BOB, {"notes": True}, "me")
    b.repo.revoke_peer_share(sid, ADA)
    st = await pull(a, b)
    assert st.merged == 1
    await pull(b, a)
    for n in (a, b):
        assert n.repo.peer_share(sid, ADA)["revoked_at"], n.name
        assert n.repo.peer_share(sid, BOB)["revoked_at"] == "", n.name
        assert n.repo.peer_share(sid, BOB)["parts"] == {"notes": True}, n.name
        assert [p["person"] for p in n.repo.peer_shares(sid)] == [BOB], n.name


async def test_an_older_peer_without_the_field_wipes_nothing(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    a.repo.put_peer_share(sid, ADA, PARTS, "me")
    await pull(b, a)
    doc = sync_state.export_session(b.repo, sid, b.id)
    assert len(doc.peer_shares) == 1
    doc.peer_shares = []  # what an older peer sends
    sync_state.write_session(b.repo, doc, local_device_id=b.repo.device_id, vv=doc.vv, bump=False)
    assert [p["person"] for p in b.repo.peer_shares(sid)] == [ADA]
    # And a shared_from meeting never reaches the other computer.
    mine = meeting(b, "Mine")
    theirs = meeting(b, "Theirs")
    _shared_from(b.repo, theirs)
    await pull(a, b)
    assert a.repo.get(mine) is not None and a.repo.get(theirs) is None


# ---- a share change is not a change to the meeting --------------------------------------------


def _updated_at(repo, sid) -> str:
    return repo._conn.execute("SELECT updated_at FROM sessions WHERE id=?", (sid,)).fetchone()[0]


def test_sharing_moves_the_change_feed_but_not_updated_at(repo):
    sid = _save(repo)
    updated, seq = _updated_at(repo, sid), _seq(repo, sid)
    vv = repo.sync_state(sid)[1]["dev-a"]
    repo.put_peer_share(sid, ADA, PARTS, "me")
    assert _updated_at(repo, sid) == updated and _seq(repo, sid) > seq
    assert repo.sync_state(sid)[1]["dev-a"] == vv + 1
    seq = _seq(repo, sid)
    repo.revoke_peer_share(sid, ADA)
    assert _updated_at(repo, sid) == updated and _seq(repo, sid) > seq
    assert repo.get(sid).updated_at.isoformat() == updated


def test_a_publication_is_not_behind_after_sharing(client, ctx, seals):  # noqa: F811
    s = _pub_meeting(ctx)
    pub = _publish(client, s.id)
    assert not pub["behind"]
    updated = _updated_at(ctx.repo, s.id)
    ctx.repo.put_peer_share(s.id, ADA, PARTS, "me")
    assert _updated_at(ctx.repo, s.id) == updated
    assert not client.get(f"/api/sessions/{s.id}/publication").json()["behind"]
    ctx.repo.revoke_peer_share(s.id, ADA)
    assert not client.get(f"/api/sessions/{s.id}/publication").json()["behind"]


def test_decision_linking_and_migration_skip_shared_from_meetings(repo):
    data = SummaryData(decisions=["Ship on Friday"], decision_ids=["d1"], decision_at=[0.0])
    mine = _save(repo, "Mine", summary_data=data.model_copy(deep=True))
    theirs = _save(repo, "Theirs", summary_data=data.model_copy(deep=True))
    _shared_from(repo, theirs)
    assert [m.id for m in decision_links._meetings(repo)] == [mine]


def test_the_task_migration_skips_shared_from_meetings(ctx, monkeypatch):
    from mnemosyne.services import task_migration

    data = SummaryData(
        decisions=["Ship"],
        decision_ids=["d1"],
        action_items=[ActionItem(id="a1", text="Send the plan")],
    )
    mine = _save(ctx.repo, "Mine", summary_data=data.model_copy(deep=True))
    theirs = _save(ctx.repo, "Theirs", summary_data=data.model_copy(deep=True))
    _shared_from(ctx.repo, theirs)
    seen = []
    monkeypatch.setattr(task_links, "link_items", lambda app, sid, *a: seen.append(sid) or [])
    monkeypatch.setattr(decision_links, "link_items", lambda app, sid, *a: seen.append(sid) or [])
    assert task_migration.migrate(ctx)["tasks_linked"] == 0
    assert seen == [mine, mine]
