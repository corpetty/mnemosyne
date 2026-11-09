"""Library sync on a computer that hosts a team (docs/plans/2026-11-09-team-hosting.md 2.4, task 5):
it carries only the owner's own meetings (owner '' or H, not a contact's), served and taken,
also after hosting is turned off; meetings that arrive are the owner's and a round never resets
an owner. And on every computer (D6): a peer's delete never removes a meeting the records keep.
Two apps in one process, as in test_sync_service.py: A hosts, B is the owner's laptop."""

import asyncio
import sqlite3
from datetime import datetime

import pytest

from mnemosyne import access
from mnemosyne.models.session import Session
from mnemosyne.services import notes_media
from mnemosyne.services.sync_peers import SyncPairingError, check_can_pair
from mnemosyne.services.sync_state import export_session
from mnemosyne.storage.sqlite import SessionRepository
from tests.test_notes_media import sha, webp
from tests.test_sync_service import meeting, nodes, pull, sidecar  # noqa: F401  (fixtures)

pytestmark = pytest.mark.anyio

HOST = "u-host"
MEMBER = "u-member"


def host(node) -> None:
    """Turn `node` into a computer that hosts a team, as team_host.turn_on leaves it."""
    node.ctx.settings.team_mode = True
    node.ctx.settings.team_owner_id = HOST
    node.ctx.follow_hosting()


def stop_hosting(node) -> None:
    """Hosting turned off: team mode goes, the owner stays (services/team_host.py)."""
    node.ctx.settings.team_mode = False
    node.ctx.follow_hosting()


@pytest.fixture
def hosted(nodes):  # noqa: F811
    a, b = nodes
    host(a)
    return a, b


def owner_of(node, sid: str) -> str:
    with access.internally():
        return node.repo.get(sid).owner_id


def member_meeting(node, name="Member's client call", **kw) -> str:
    return meeting(node, name=name, owner_id=MEMBER, **kw)


async def served(b, a) -> str:
    """What A answers B's GET /api/sync/changes with, as text (from the start)."""
    async with b.sync.client_for(b.peer(a)) as c:
        r = await c.get("/api/sync/changes", params={"limit": 200})
    assert r.status_code == 200
    return r.text


# ---- I4: a peer of a host gets only the owner's meetings -----------------------------------


async def test_a_peer_of_a_host_receives_only_the_owners_meetings(hosted):
    a, b = hosted
    mine = meeting(a, name="Owner's planning")
    assert owner_of(a, mine) == HOST
    theirs = member_meeting(a)
    gone_member = member_meeting(a, name="Member's deleted call")
    gone_mine = meeting(a, name="Owner's deleted call")
    with access.internally():
        a.ctx.sessions.delete_session(gone_member)
        a.ctx.sessions.delete_session(gone_mine)
    st = await pull(b, a)
    assert st.state == "done" and st.total == 1
    assert b.repo.get(mine) is not None and b.repo.get(theirs) is None
    assert b.repo.has_tombstone(gone_mine) and not b.repo.has_tombstone(gone_member)
    text = await served(b, a)
    assert "Member" not in text and theirs not in text and gone_member not in text
    assert "Owner's deleted call" in text
    # The owner goes out as '': the laptop is a plain computer, its meetings have no owner.
    assert owner_of(b, mine) == ""
    assert export_session(a.repo, mine, a.id, HOST).owner_id == ""


async def test_a_member_meetings_image_is_not_served(hosted):
    a, b = hosted
    mine, theirs = meeting(a), member_meeting(a)
    data = webp(pad=b"pixels")
    for sid in (mine, theirs):
        notes_media.store(a.ctx, sid, sha(data), data)
    async with b.sync.client_for(b.peer(a)) as c:
        assert (await c.get(f"/api/sync/media/{mine}/{sha(data)}")).status_code == 200
        assert (await c.get(f"/api/sync/media/{theirs}/{sha(data)}")).status_code == 404


async def test_a_hosts_changes_page_counts_only_the_owners_meetings(hosted):
    a, b = hosted
    for n in range(3):
        member_meeting(a, name=f"Member {n}")
    mine = [meeting(a, name=f"Mine {n}") for n in range(3)]
    a.sync.page_size = b.sync.page_size = 2
    st = await pull(b, a)
    assert st.total == 3 and st.received == 3
    assert sorted(s.id for s in b.repo.list_summaries()) == sorted(mine)


# ---- I5: what arrives never changes or deletes a member's meeting --------------------------


async def test_a_doc_or_tombstone_naming_a_member_meeting_changes_nothing(hosted):
    a, b = hosted
    theirs = member_meeting(a)
    created = a.repo.get(theirs).created_at
    # B has a meeting under that id (same start: not an ordinary id clash).
    b.repo.save(Session(id=theirs, name="Overwrite it", created_at=created, notes="B's\n"))
    st = await pull(a, b)
    assert st.clashes == {theirs: "Overwrite it"}
    got = a.repo.get(theirs)
    assert (got.name, got.owner_id, got.notes) == (
        "Member's client call",
        MEMBER,
        "Intro.\nPlan.\n",
    )
    # B deletes it: A's member meeting stays, and A keeps no tombstone that would pass it on.
    with access.internally():
        b.ctx.sessions.delete_session(theirs)
    a.sync.states.clear()  # even without the clash remembered
    st = await pull(a, b)
    assert st.deleted == 0 and a.repo.get(theirs) is not None
    assert not a.repo.has_tombstone(theirs)
    # Taken directly too (a page could carry it without a clash first).
    doc = export_session(a.repo, theirs, a.id).model_copy(update={"name": "Changed"})
    out = await asyncio.to_thread(a.sync.take, a.peer(b), doc)
    assert out.kind == "clash" and a.repo.get(theirs).name == "Member's client call"


# ---- I6: what arrives is the owner's, and stays so ------------------------------------------


async def test_meetings_arriving_on_a_host_belong_to_its_owner(hosted, sidecar):  # noqa: F811
    a, b = hosted
    laptop = meeting(b, name="Recorded on the laptop")
    await pull(a, b)
    assert owner_of(a, laptop) == HOST
    # Edits on both sides (a merge) and one side only (a fast-forward) keep it the owner's.
    a.repo.update_fields(laptop, name="Renamed on A")
    b.repo.update_fields(laptop, notes="Edited on B.\n")
    st = await pull(a, b)
    assert st.merged == 1 and owner_of(a, laptop) == HOST
    await pull(b, a)
    b.repo.update_fields(laptop, notes="Edited on B again.\n")
    st = await pull(a, b)
    assert st.updated == 1 and owner_of(a, laptop) == HOST
    # The owner's meeting made on A comes back from B without one: still the owner's.
    mine = meeting(a)
    await pull(b, a)
    b.repo.update_fields(mine, name="Renamed on B")
    st = await pull(a, b)
    assert st.updated == 1 and owner_of(a, mine) == HOST
    assert a.repo.get(mine).name == "Renamed on B"


# ---- I14: after hosting is turned off ------------------------------------------------------


async def test_members_meetings_never_flow_after_hosting_is_off(hosted):
    a, b = hosted
    theirs = member_meeting(a)
    gone = member_meeting(a, name="Member's deleted call")
    with access.internally():
        a.ctx.sessions.delete_session(gone)
    data = webp(pad=b"pixels")
    notes_media.store(a.ctx, theirs, sha(data), data)
    stop_hosting(a)
    assert a.ctx.settings.team_owner_id == HOST and not a.ctx.settings.team_mode
    later = meeting(a, name="After hosting")  # unowned again: still the owner's
    a.repo.update_fields(theirs, name="Member's call, edited")
    st = await pull(b, a)
    assert st.state == "done"
    assert b.repo.get(later) is not None and b.repo.get(theirs) is None
    assert not b.repo.has_tombstone(gone)
    text = await served(b, a)
    assert "Member" not in text
    async with b.sync.client_for(b.peer(a)) as c:
        assert (await c.get(f"/api/sync/media/{theirs}/{sha(data)}")).status_code == 404


# ---- I13 (D6): a peer's delete never removes what the records keep -------------------------


def kept_events(node, sid: str) -> list[dict]:
    return [e for e in node.repo.events(sid) if e["kind"] == "kept_despite_peer_delete"]


@pytest.mark.parametrize("rule", ["hold", "period"])
async def test_a_peers_delete_keeps_a_meeting_the_records_keep(nodes, rule):  # noqa: F811
    a, b = nodes  # plain computers: no team anywhere
    sid = meeting(a)
    await pull(b, a)
    if rule == "hold":
        a.repo.set_legal_hold(sid, "Dispute 12")
    else:
        a.ctx.settings.records_retention_years = 7
    with access.internally():
        b.ctx.sessions.delete_session(sid)
    st = await pull(a, b)
    assert st.deleted == 0 and a.repo.get(sid) is not None
    assert not a.repo.has_tombstone(sid)  # not passed on
    (event,) = kept_events(a, sid)
    assert event["detail"]["source"] == "B"
    assert ("Dispute 12" if rule == "hold" else "Kept as a record until") in event["detail"][
        "reason"
    ]
    # Later rounds read the tombstone again (the margin): logged once.
    await pull(a, b)
    assert len(kept_events(a, sid)) == 1 and a.repo.get(sid) is not None
    assert sid not in {t["session_id"] for t in a.repo.tombstones_since("")}


async def test_a_peers_delete_goes_ahead_and_is_logged_without_a_records_rule(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    with access.internally():
        b.ctx.sessions.delete_session(sid)
    st = await pull(a, b)
    assert st.deleted == 1 and a.repo.get(sid) is None
    (entry,) = [d for d in a.repo.deletions() if d["session_id"] == sid]
    assert (entry["what"], entry["by"]) == ("meeting", "Sync (B)")


# ---- I1: a dedicated team server still refuses ---------------------------------------------


async def test_a_dedicated_team_server_refuses_sync_and_a_host_does_not(nodes):  # noqa: F811
    a, b = nodes
    a.ctx.settings.team_mode = True
    a.ctx.settings.team_owner_id = ""
    async with a.client() as c:
        assert (await c.get("/api/sync/hello")).status_code == 409
        assert (await c.post("/api/pairing/codes", json={"kind": "sync"})).status_code == 409
    with pytest.raises(SyncPairingError):
        check_can_pair(a.ctx)
    host(a)
    async with a.client() as c:
        assert (await c.get("/api/sync/hello")).status_code == 200
    assert (await pull(b, a)).state == "done"


# ---- tombstones from before they kept an owner ---------------------------------------------


@pytest.mark.parametrize("had_owners", [False, True])
def test_old_tombstones_get_an_owner_by_what_the_database_had(tmp_path, had_owners):
    path = tmp_path / "db.sqlite"
    repo = SessionRepository(path)
    repo.save(Session(id="aaaaaaaaaaaa", name="Kept", owner_id=MEMBER if had_owners else ""))
    repo.add_tombstone("bbbbbbbbbbbb", datetime.now().isoformat(), "dev", "Gone")
    repo.close()
    with sqlite3.connect(path) as conn:  # as an older Mnemosyne left it
        conn.execute("ALTER TABLE sync_tombstones DROP COLUMN owner_id")
    repo = SessionRepository(path)
    with sqlite3.connect(path) as conn:
        (owner,) = conn.execute("SELECT owner_id FROM sync_tombstones").fetchone()
    assert owner == ("?" if had_owners else "")
    # Without a team owner every tombstone is served, with one only the owner's.
    assert len(repo.tombstones_since("")) == 1
    assert len(repo.tombstones_since("", HOST)) == (0 if had_owners else 1)
    repo.close()
