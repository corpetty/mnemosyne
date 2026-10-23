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
