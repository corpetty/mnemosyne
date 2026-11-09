"""Publications in library sync (services/sync_publications.py, models/sync_session.py
`merge_publications`, phase 7 P8): two apps in this process pulling from each other (the pattern
of test_sync_service), one fake publish host shared through httpx.MockTransport."""

import time
from datetime import datetime, timedelta

import anyio
import httpx
import pytest

from mnemosyne.models.session import TranscriptSegment
from mnemosyne.models.sync_session import SyncPublication, merge_publications
from mnemosyne.services import publication_format as pf
from mnemosyne.services import publishing
from tests.test_publishing import FakeHost, fake_cut  # noqa: F401
from tests.test_sync_service import Node, meeting, nodes, pull, sidecar  # noqa: F401

pytestmark = pytest.mark.anyio

HOST = "https://pub.example.org"
QUOTE = {"start": 0, "end": 1, "with_clip": True}


class Host(FakeHost):
    """FakeHost that also serves blobs to anyone (readers, and a computer keeping a clip)."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if request.method == "GET" and len(parts) == 3 and parts[2] in self.blobs:
            self.calls.append((request.method, request.url.path, dict(request.headers)))
            return httpx.Response(200, content=self.blobs[parts[2]])
        return super().handler(request)


@pytest.fixture
def host(nodes):  # noqa: F811
    h = Host()
    for n in nodes:
        n.ctx.http_transport = httpx.MockTransport(h.handler)
        n.ctx.settings.publish_host = HOST
    return h


async def _job(node: Node, response: httpx.Response, c: httpx.AsyncClient) -> dict:
    jid = response.json()["id"]
    for _ in range(500):
        job = (await c.get(f"/api/jobs/{jid}")).json()
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        await anyio.sleep(0.02)
    raise AssertionError("the publish job did not finish")


async def publish(node: Node, sid: str, **sel) -> dict:
    async with node.client() as c:
        r = await c.put(f"/api/sessions/{sid}/publication", json={"where": "local", **sel})
        assert r.status_code == 200, r.text
        job = await _job(node, r, c)
    assert job["status"] == "completed", job
    return job["result"]["publication"]


async def put_refused(node: Node, sid: str, **sel) -> httpx.Response:
    async with node.client() as c:
        return await c.put(f"/api/sessions/{sid}/publication", json={"where": "local", **sel})


async def shown(node: Node, sid: str) -> dict | None:
    async with node.client() as c:
        return (await c.get(f"/api/sessions/{sid}/publication")).json()


async def unpublish(node: Node, sid: str) -> httpx.Response:
    async with node.client() as c:
        return await c.delete(f"/api/sessions/{sid}/publication")


def talk(node: Node) -> str:
    lines = [("Ann", 0.0, 1.0), ("Ann", 2.0, 3.0), ("Bo", 4.0, 5.0)]
    return meeting(
        node,
        transcript=[
            TranscriptSegment(text=f"line {i}", speaker=sp, start=a, end=b)
            for i, (sp, a, b) in enumerate(lines)
        ],
        summary="Ann and Bo met.",
    )


def row(node: Node, sid: str, live: bool = True) -> dict:
    rows = node.repo.session_publications(sid)
    rows = [r for r in rows if bool(r["unpublished_at"]) != live]
    assert len(rows) == 1, rows
    return rows[0]


def folder(node: Node, pub_id: str):
    return publishing.pub_dir(node.ctx, pub_id)


# ---- publish on A, B sees it -----------------------------------------------------------------


async def test_a_local_publication_reaches_b_which_does_not_serve_it(nodes):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid)
    assert pub["served_by"] is None and pub["url"]
    await pull(b, a)

    seen = await shown(b, sid)
    assert seen["id"] == pub["id"] and seen["version"] == 1 and seen["where"] == "local"
    assert seen["served_by"] == "A" and seen["url"] is None
    assert not folder(b, pub["id"]).exists()  # the files are on A
    async with b.client() as c:
        assert (await c.get(f"/pub/{pub['id']}")).status_code == 404
    # The key and the token came with it: the row is the same on both.
    ra, rb = row(a, sid), row(b, sid)
    assert rb["key"] == ra["key"] and rb["token"] == ra["token"]
    assert rb["origin"] == a.id and ra["origin"] == ""
    assert {k: rb[k] for k in ("version", "selection", "created_at", "updated_at")} == {
        k: ra[k] for k in ("version", "selection", "created_at", "updated_at")
    }
    # Back to A: still its own.
    await pull(a, b)
    assert row(a, sid)["origin"] == "" and (await shown(a, sid))["served_by"] is None


async def test_the_key_travels_so_b_can_make_the_hosted_link(nodes, host, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid, where="host", expires_days=30)
    await pull(b, a)
    on_b = await shown(b, sid)
    assert on_b["url"] == pub["url"] and on_b["url"].startswith(f"{HOST}/p/{pub['id']}#")
    assert pf.decode_key(on_b["url"].split("#")[1]) == row(a, sid)["key"]
    assert on_b["served_by"] is None


async def test_images_of_a_hosted_publication_travel_and_b_reuses_them(nodes, host):  # noqa: F811
    from mnemosyne.services import notes_media
    from tests.test_notes_media import sha, value, webp

    a, b = nodes
    data = webp(64, 48)
    sid = talk(a)
    info = notes_media.store(a.ctx, sid, sha(data), data)
    a.repo.update_fields(
        sid, notes=f"Intro\n![Board](mnemosyne:image/{value(data, info.ext, 64, 48)})\n"
    )
    pub = await publish(a, sid, where="host", expires_days=30, notes=True, attendees=["Ann"])
    (image,) = [k for k in row(a, sid)["clip_blobs"] if k.startswith("image:")]
    assert image == f"image:{sha(data)}" and len(host.blobs) == 1
    blob_sha = next(iter(host.blobs))

    await pull(b, a)
    assert row(b, sid)["clip_blobs"] == row(a, sid)["clip_blobs"]
    assert not folder(b, pub["id"]).exists()
    # B has the notes but not the sealed image: it fetches it from the host, seals nothing new.
    host.calls.clear()
    upd = await publish(b, sid, where="host", expires_days=30, notes=True, attendees=["Ann"])
    assert upd["version"] == 2 and set(host.blobs) == {blob_sha}
    assert ("GET", f"/pub/{pub['id']}/{blob_sha}") in [(m, p) for m, p, _ in host.calls]
    assert [(m, p) for m, p, _ in host.calls if m == "PUT"] == [("PUT", f"/pub/{pub['id']}")]
    key = row(b, sid)["key"]
    m = pf.open_manifest(pub["id"], 2, host.manifest, key)
    assert [(i.id, i.sha256, i.v) for i in m.images] == [(sha(data), blob_sha, 1)]
    await pull(a, b)
    assert row(a, sid)["clip_blobs"] == row(b, sid)["clip_blobs"]


# ---- hosted: either machine ------------------------------------------------------------------


async def test_a_hosted_publication_is_updated_from_b_and_reaches_a(nodes, host, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid, where="host", expires_days=30, quotes=[QUOTE])
    assert len(host.blobs) == 1 and fake_cut == [(0, 0)]
    clip_sha = next(iter(host.blobs))
    await pull(b, a)
    assert not folder(b, pub["id"]).exists()

    # B has neither the audio nor the clip's blob: it fetches the blob from the host.
    host.calls.clear()
    upd = await publish(b, sid, where="host", expires_days=30, quotes=[QUOTE], summary=False)
    assert upd["id"] == pub["id"] and upd["version"] == 2 and upd["url"] == pub["url"]
    assert fake_cut == [(0, 0)]  # nothing cut again
    assert ("GET", f"/pub/{pub['id']}/{clip_sha}") in [(m, p) for m, p, _ in host.calls]
    assert [(m, p) for m, p, _ in host.calls if m == "PUT"] == [("PUT", f"/pub/{pub['id']}")]
    assert host.version == 2
    assert (folder(b, pub["id"]) / "blobs" / clip_sha).is_file()  # kept here for file exports
    assert [e["kind"] for e in b.repo.events(sid) if e["kind"].startswith("publication")] == [
        "publication_updated"
    ]

    await pull(a, b)
    ra = row(a, sid)
    assert ra["version"] == 2 and ra["selection"]["summary"] is False
    assert ra["clip_blobs"] == row(b, sid)["clip_blobs"]
    # A's files were of version 1: gone (an update from A fetches what it needs).
    assert not folder(a, pub["id"]).exists()
    assert (await shown(a, sid))["version"] == 2
    again = await publish(a, sid, where="host", expires_days=30, quotes=[QUOTE])
    assert again["version"] == 3 and host.version == 3


async def test_b_cannot_add_a_clip_the_audio_is_on_a(nodes, host, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    await publish(a, sid, where="host", expires_days=30, quotes=[QUOTE])
    await pull(b, a)
    other = {"start": 4, "end": 5, "with_clip": True}
    r = await put_refused(b, sid, where="host", expires_days=30, quotes=[QUOTE, other])
    assert r.status_code == 409 and r.json()["detail"] == "The audio is on A"
    # Without the new clip it is fine.
    assert (await publish(b, sid, where="host", expires_days=30, quotes=[QUOTE]))["version"] == 2


# ---- "this computer": its machine only -------------------------------------------------------


async def test_a_local_publication_cannot_be_changed_from_b(nodes):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid)
    await pull(b, a)

    r = await put_refused(b, sid)
    assert r.status_code == 409 and r.json()["detail"] == "Published from A"
    r = await unpublish(b, sid)
    assert r.status_code == 409 and r.json()["detail"] == "Published from A"
    assert row(b, sid)["version"] == 1 and not row(b, sid)["unpublished_at"]
    # Nothing changed on A either.
    await pull(a, b)
    assert row(a, sid)["version"] == 1 and not row(a, sid)["unpublished_at"]
    async with a.client() as c:
        assert (await c.get(f"/pub/{pub['id']}")).status_code == 200


async def test_unpublishing_on_a_removes_the_files_on_b(nodes, host, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid, where="host", expires_days=30, quotes=[QUOTE])
    await pull(b, a)
    await publish(b, sid, where="host", expires_days=30, quotes=[QUOTE])  # B holds files now
    assert (folder(b, pub["id"]) / "manifest").is_file()
    await pull(a, b)

    assert (await unpublish(a, sid)).status_code == 200
    assert host.manifest is None
    await pull(b, a)
    assert row(b, sid, live=False)["unpublished_at"]
    assert await shown(b, sid) is None
    assert not (folder(b, pub["id"]) / "manifest").exists()
    assert not (folder(b, pub["id"]) / "blobs").exists()
    assert publishing.read_meta(b.ctx, pub["id"])["gone"] == "unpublished"
    async with b.client() as c:
        assert (await c.get(f"/pub/{pub['id']}")).status_code == 410
    # Unpublishing again from B finds nothing live; a new publication is a new link.
    assert (await unpublish(b, sid)).status_code == 404
    again = await publish(b, sid)
    assert again["id"] != pub["id"] and again["served_by"] is None


async def test_unpublishing_a_local_publication_on_a_reaches_b(nodes):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid)
    await pull(b, a)
    assert (await unpublish(a, sid)).status_code == 200
    await pull(b, a)
    assert await shown(b, sid) is None and row(b, sid, live=False)["unpublished_at"]
    assert not folder(b, pub["id"]).exists()  # never had files, none made
    # B can publish it now (its own, served by B: the meeting is A's but no clips).
    mine = await publish(b, sid)
    assert mine["id"] != pub["id"] and mine["served_by"] is None
    await pull(a, b)
    assert (await shown(a, sid))["served_by"] == "B"
    assert (await put_refused(a, sid)).status_code == 409


async def test_deleting_the_meeting_removes_the_files_on_b(nodes, host, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid, where="host", expires_days=30)
    await pull(b, a)
    await publish(b, sid, where="host", expires_days=30)  # files on B
    await pull(a, b)
    assert folder(b, pub["id"]).exists()
    a.ctx.sessions.delete_session(sid)
    await pull(b, a)
    assert b.repo.get(sid) is None
    assert not (folder(b, pub["id"]) / "manifest").exists()
    assert publishing.read_meta(b.ctx, pub["id"])["gone"] == "deleted"


# ---- concurrent changes ----------------------------------------------------------------------


async def test_concurrent_updates_the_second_is_refused_then_sync_settles(  # noqa: F811
    nodes,  # noqa: F811
    host,
    sidecar,  # noqa: F811
    fake_cut,  # noqa: F811
):
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid, where="host", expires_days=30)
    await pull(b, a)
    # Both update without hearing of each other. A's version 2 reaches the host first; B's
    # version 2 is refused there instead of being taken as done (the host holds A's manifest).
    await publish(a, sid, where="host", expires_days=30, summary=False)
    async with b.client() as c:
        r = await c.put(
            f"/api/sessions/{sid}/publication",
            json={"where": "host", "expires_days": 30, "topics": False},
        )
        job = await _job(b, r, c)
    assert job["status"] == "failed" and "at the same time" in job["error"]
    assert row(b, sid)["version"] == 1  # nothing recorded for the refused update
    await pull(b, a)
    rb = row(b, sid)
    assert rb["id"] == pub["id"] and rb["version"] == 2
    assert rb["selection"]["summary"] is False and rb["selection"]["topics"] is True
    await pull(a, b)
    ra = row(a, sid)
    assert (ra["version"], ra["selection"], ra["updated_at"]) == (
        rb["version"],
        rb["selection"],
        rb["updated_at"],
    )
    assert len(a.repo.session_publications(sid)) == 1


async def test_the_version_is_the_larger_of_the_two(nodes, host, sidecar, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    await publish(a, sid, where="host", expires_days=30)
    await pull(b, a)
    ra, rb = row(a, sid), row(b, sid)
    later = (datetime.fromisoformat(ra["updated_at"]) + timedelta(seconds=5)).isoformat()
    # A went on to version 3; B's version 2 is the later change.
    a.repo.put_publication({**ra, "version": 3})
    b.repo.put_publication(
        {**rb, "version": 2, "updated_at": later, "selection": {**rb["selection"], "title": "B"}}
    )
    await pull(b, a)
    assert (row(b, sid)["version"], row(b, sid)["selection"]["title"]) == (3, "B")
    await pull(a, b)
    assert (row(a, sid)["version"], row(a, sid)["selection"]["title"]) == (3, "B")


async def test_two_publications_of_one_meeting_leave_one_live(nodes, host, sidecar, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    await pull(b, a)  # B knows the meeting
    first = await publish(a, sid, where="host", expires_days=30)
    second = await publish(b, sid)  # later, "this computer" (B)
    assert first["id"] != second["id"]
    assert host.manifest is not None

    await pull(b, a)
    assert (await shown(b, sid))["id"] == second["id"]
    assert row(b, sid, live=False)["id"] == first["id"]
    # The loser was on the host: B queued its deletion and did it.
    assert host.manifest is None
    await pull(a, b)
    assert (await shown(a, sid))["id"] == second["id"]
    assert (await shown(a, sid))["served_by"] == "B"
    assert row(a, sid, live=False)["id"] == first["id"]
    assert row(a, sid, live=False)["unpublished_at"]


def test_merge_publications_rules():
    def pub(id_, at, version=1, unpublished=None, **kw):
        t = datetime(2026, 10, 23, 12, 0, 0) + timedelta(minutes=at)
        return SyncPublication(
            id=id_,
            key="a2V5",
            token="t",
            where="local",
            version=version,
            selection={},
            created_at=t.isoformat(),
            updated_at=t.isoformat(),
            unpublished_at=t.isoformat() if unpublished else None,
            **kw,
        )

    # Newer wins whole, version is the max.
    out = merge_publications([pub("p", 5, 4)], [pub("p", 9, 2, published_by="B")])
    assert [(p.version, p.published_by) for p in out] == [(4, "B")]
    # Unpublished beats live at an equal time.
    out = merge_publications([pub("p", 5, 2)], [pub("p", 5, 2, unpublished=True)])
    assert out[0].unpublished_at
    out = merge_publications([pub("p", 5, 2, unpublished=True)], [pub("p", 5, 2)])
    assert out[0].unpublished_at
    # Order: ours first, then theirs we lack; an unpublish stays an unpublish.
    out = merge_publications([pub("a", 1, unpublished=True)], [pub("b", 2), pub("a", 1)])
    assert [p.id for p in out] == ["a", "b"]
    # Two live: the later stays.
    out = merge_publications([pub("a", 1)], [pub("b", 2)])
    live = [p for p in out if not p.unpublished_at]
    assert [p.id for p in live] == ["b"]
    dead = next(p for p in out if p.id == "a")
    assert dead.unpublished_at == dead.updated_at and dead.updated_at > pub("a", 1).updated_at


async def test_an_expiry_makes_no_new_version(nodes, host, fake_cut):  # noqa: F811
    """Expiry is worked out on each machine: it makes no new version of the meeting."""
    a, b = nodes
    sid = talk(a)
    await publish(a, sid, where="host", expires_days=30)
    await pull(b, a)
    r = row(a, sid)
    gone = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    a.repo.put_publication({**r, "expires_at": gone})
    before = a.repo.sync_state(sid)[1]
    publishing.sweep_expired(a.ctx)
    assert a.repo.sync_state(sid)[1] == before
    assert row(a, sid, live=False)["unpublished_at"]


async def test_publishing_touches_the_meeting_so_it_travels(nodes):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    await pull(b, a)
    before = a.repo.sync_state(sid)[1]
    time.sleep(0.01)
    await publish(a, sid)
    assert a.repo.sync_state(sid)[1][a.id] == before[a.id] + 1
    await unpublish(a, sid)
    assert a.repo.sync_state(sid)[1][a.id] == before[a.id] + 2


# ---- the owner keys of a live publication (task 4) --------------------------------------------


async def test_the_owner_keys_travel_with_the_row(nodes, host, fake_cut):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    pub = await publish(a, sid, where="host", expires_days=30, follow=True, comments=True)
    assert "sign_key" not in pub and "box_key" not in pub
    ra = row(a, sid)
    assert len(ra["sign_key"]) == 32 and len(ra["box_key"]) == 32
    await pull(b, a)
    rb = row(b, sid)
    assert rb["sign_key"] == ra["sign_key"] and rb["box_key"] == ra["box_key"]
    assert rb["selection"]["follow"] and rb["selection"]["comments"]
    # B's update keeps the pair; it goes back to A unchanged.
    await publish(b, sid, where="host", expires_days=30, follow=True)
    assert row(b, sid)["sign_key"] == ra["sign_key"]
    await pull(a, b)
    assert row(a, sid)["sign_key"] == ra["sign_key"] and row(a, sid)["version"] == 2
    # A publication without them carries none.
    other = talk(a)
    await publish(a, other)
    assert row(a, other)["sign_key"] is None
    await pull(b, a)
    assert row(b, other)["sign_key"] is None and row(b, other)["box_key"] is None


def test_a_merge_never_drops_keys_the_newer_row_lacks():
    def pub(updated, sign=""):
        return SyncPublication(
            id="p1",
            key="a2V5",
            token="t",
            where="host",
            version=1,
            selection={},
            created_at="2026-11-05T09:00:00+00:00",
            updated_at=updated,
            sign_key=sign,
            box_key="Ym94" if sign else "",
        )

    old = pub("2026-11-05T09:00:00+00:00", sign="c2lnbg==")
    new = pub("2026-11-05T10:00:00+00:00")
    for ours, theirs in ((old, new), (new, old)):
        (merged,) = merge_publications([ours], [theirs])
        assert merged.updated_at == new.updated_at
        assert (merged.sign_key, merged.box_key) == ("c2lnbg==", "Ym94")


# ---- the block list of a live publication (M4, task 3) ----------------------------------------

RID = "ab" * 8
RID2 = "cd" * 8


async def test_a_block_travels_and_a_later_unblock_on_the_other_computer_wins(nodes, host):  # noqa: F811
    a, b = nodes
    sid = talk(a)
    await publish(a, sid, where="host", expires_days=30, comments=True)
    pub_id = row(a, sid)["id"]
    await pull(b, a)
    # Blocked on A (as the owner's route does: no new publication, updated_at kept).
    before = a.repo.sync_state(sid)[1]
    updated = row(a, sid)["updated_at"]
    a.repo.set_blocked_reader(pub_id, RID, True, "2026-11-09T10:00:00+00:00", "Ana")
    assert row(a, sid)["updated_at"] == updated
    assert a.repo.sync_state(sid)[1][a.id] == before[a.id] + 1
    await pull(b, a)
    assert row(b, sid)["blocked_readers"][RID] == {
        "at": "2026-11-09T10:00:00+00:00",
        "on": True,
        "name": "Ana",
    }
    # Unblocked on B later, and another reader blocked on A meanwhile: both survive the meeting.
    b.repo.set_blocked_reader(pub_id, RID, False, "2026-11-09T11:00:00+00:00", "Ana")
    a.repo.set_blocked_reader(pub_id, RID2, True, "2026-11-09T11:30:00+00:00", "Bo")
    await pull(a, b)
    await pull(b, a)
    for node in (a, b):
        blocked = row(node, sid)["blocked_readers"]
        assert blocked[RID]["on"] is False and blocked[RID2]["on"] is True
    # Nothing else changed: the selection and version are as published.
    assert row(a, sid)["version"] == 1 and row(a, sid)["selection"]["comments"] is True


def test_merge_blocked_rules():
    def pub(at, blocked, **kw):
        t = datetime(2026, 11, 9, 12, 0, 0) + timedelta(minutes=at)
        return SyncPublication(
            id="p",
            key="a2V5",
            token="t",
            where="host",
            version=1,
            selection={},
            created_at=t.isoformat(),
            updated_at=t.isoformat(),
            blocked_readers=blocked,
            **kw,
        )

    def e(at, on, name=""):
        return {"at": f"2026-11-09T{at}+00:00", "on": on, "name": name}

    ours = pub(9, {RID: e("10:00", True, "Ana")})  # the newer row, with the older block state
    theirs = pub(1, {RID: e("11:00", False, "Ana"), RID2: e("09:00", True, "Bo")})
    for a, b in ((ours, theirs), (theirs, ours)):
        (merged,) = merge_publications([a], [b])
        assert merged.updated_at == ours.updated_at  # the newer row still wins the rest
        assert merged.blocked_readers[RID]["on"] is False  # the later unblock wins per reader
        assert merged.blocked_readers[RID2]["on"] is True  # and the union is kept
    # At the same instant a block wins: a tie never lets a blocked reader in.
    tie_a, tie_b = pub(1, {RID: e("10:00", True)}), pub(1, {RID: e("10:00", False)})
    for a, b in ((tie_a, tie_b), (tie_b, tie_a)):
        assert merge_publications([a], [b])[0].blocked_readers[RID]["on"] is True
    # An older peer sends no list: ours stays.
    (merged,) = merge_publications([ours], [pub(1, {})])
    assert merged.blocked_readers == ours.blocked_readers
