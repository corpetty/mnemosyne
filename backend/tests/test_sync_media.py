"""The images of the notes in library sync (docs/plans/2026-11-04-images-tasks.md, task 10): the
document carries rows, a pull fetches the bytes from the peer that served the page
(`GET /api/sync/media/...`), a blob a fold finds missing is asked for in the background, a
meeting deleted on a peer takes its folder with it. Two apps in one process as in
test_sync_service.py."""

import asyncio

import pytest

from mnemosyne.models.sync_session import SyncSession
from mnemosyne.services import notes_media
from mnemosyne.services.sync_state import export_session
from mnemosyne.storage.crypto import new_key
from tests.test_notes_media import jpeg, sha, webp
from tests.test_sync_service import meeting, nodes, pull  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio


def files(node, sid: str) -> list[str]:
    folder = node.ctx.settings.notes_media_dir / sid
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def media_shas(node, sid: str) -> set[str]:
    return {r["sha256"] for r in node.repo.media_rows(sid)}


async def test_an_image_moves_from_a_to_b_and_is_sealed_there(nodes):  # noqa: F811
    a, b = nodes
    b.ctx.master_key = new_key()
    b.ctx.settings.encrypt_at_rest = True
    sid = meeting(a)
    data = webp(pad=b"pixels of A")
    notes_media.store(a.ctx, sid, sha(data), data, by="Ann")
    await pull(b, a)
    assert media_shas(b, sid) == {sha(data)}
    (row,) = b.repo.media_rows(sid)
    assert (row["ext"], row["width"], row["height"], row["size"], row["by"]) == (
        "webp",
        640,
        480,
        len(data),
        "Ann",
    )
    assert files(b, sid) == [f"{sha(data)}.webp.enc"]  # sealed on B
    assert files(a, sid) == [f"{sha(data)}.webp"]
    assert notes_media.read(b.ctx, sid, sha(data)) == data
    assert b.repo.sync_state(sid)[1] == a.repo.sync_state(sid)[1]  # arrival is not a new version


async def test_a_blob_added_on_b_travels_back(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    first = webp(pad=b"one")
    notes_media.store(a.ctx, sid, sha(first), first)
    await pull(b, a)
    second = jpeg(pad=b"two")
    notes_media.store(b.ctx, sid, sha(second), second, by="Bea")
    await pull(a, b)
    assert media_shas(a, sid) == {sha(first), sha(second)}
    assert notes_media.read(a.ctx, sid, sha(second)) == second
    assert notes_media.read(a.ctx, sid, sha(first)) == first
    assert {r["by"] for r in a.repo.media_rows(sid)} == {"", "Bea"}
    await pull(b, a)  # nothing is left to move
    assert files(b, sid) == sorted([f"{sha(first)}.webp", f"{sha(second)}.jpeg"])


async def test_both_sides_add_a_blob_at_once(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    one, two = webp(pad=b"on A"), webp(pad=b"on B")
    notes_media.store(a.ctx, sid, sha(one), one)
    notes_media.store(b.ctx, sid, sha(two), two)
    await pull(b, a)  # concurrent: a merge
    await pull(a, b)
    for n in (a, b):
        assert media_shas(n, sid) == {sha(one), sha(two)}
        assert notes_media.read(n.ctx, sid, sha(one)) == one
        assert notes_media.read(n.ctx, sid, sha(two)) == two


async def test_a_deleted_meeting_takes_its_folder_with_it(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    data = webp(pad=b"gone soon")
    notes_media.store(a.ctx, sid, sha(data), data)
    await pull(b, a)
    assert files(b, sid)
    async with a.client() as c:
        assert (await c.delete(f"/api/sessions/{sid}")).status_code == 200
    assert files(a, sid) == []
    st = await pull(b, a)
    assert st.deleted == 1 and b.repo.get(sid) is None
    assert files(b, sid) == [] and not (b.ctx.settings.notes_media_dir / sid).exists()
    assert b.repo.media_rows(sid) == []


async def test_an_older_peer_sends_no_media_and_ours_stay(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    data = webp(pad=b"mine")
    notes_media.store(b.ctx, sid, sha(data), data)
    # A's edit arrives as an older Mnemosyne would send it: no `media` at all.
    a.repo.update_fields(sid, name="Renamed on A")
    doc = export_session(a.repo, sid, a.id).model_dump(mode="json")
    doc.pop("media")
    old = SyncSession.model_validate(doc)
    assert old.media is None
    out = await asyncio.to_thread(b.sync.take, b.peer(a), old)
    assert out.kind in ("updated", "merged")
    assert b.repo.get(sid).name == "Renamed on A"
    assert media_shas(b, sid) == {sha(data)}
    assert notes_media.read(b.ctx, sid, sha(data)) == data


async def test_an_older_peers_concurrent_edit_merges_and_keeps_ours(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    await pull(b, a)
    data = webp(pad=b"mine")
    notes_media.store(b.ctx, sid, sha(data), data)
    a.repo.update_fields(sid, name="Renamed on A")
    doc = export_session(a.repo, sid, a.id).model_copy(update={"media": None})
    out = await asyncio.to_thread(b.sync.take, b.peer(a), doc)
    assert out.kind == "merged"
    assert media_shas(b, sid) == {sha(data)}
    assert files(b, sid) == [f"{sha(data)}.webp"]


async def test_the_media_route_is_for_a_synced_computer_only(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    data = jpeg(pad=b"route")
    notes_media.store(a.ctx, sid, sha(data), data)
    url = f"/api/sync/media/{sid}/{sha(data)}"
    async with a.client() as admin:
        assert (await admin.get(url)).status_code == 403
    async with a.client(token=b.peer(a).token) as peer:
        got = await peer.get(url)
        assert got.status_code == 200 and got.content == data
        assert got.headers["content-type"] == "image/jpeg"
        assert (await peer.get(f"/api/sync/media/{sid}/{'0' * 64}")).status_code == 404
        assert (await peer.get(f"/api/sync/media/{sid}/nothex")).status_code == 404
        assert (await peer.get(f"/api/sync/media/nosuch/{sha(data)}")).status_code == 404


async def test_the_route_serves_a_sealed_blob_decrypted(nodes):  # noqa: F811
    a, b = nodes
    a.ctx.master_key = new_key()
    a.ctx.settings.encrypt_at_rest = True
    sid = meeting(a)
    data = webp(pad=b"sealed on A")
    notes_media.store(a.ctx, sid, sha(data), data)
    assert files(a, sid) == [f"{sha(data)}.webp.enc"]
    await pull(b, a)
    assert files(b, sid) == [f"{sha(data)}.webp"]
    assert notes_media.read(b.ctx, sid, sha(data)) == data


async def test_bytes_that_do_not_match_are_not_kept(nodes, monkeypatch):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    data = webp(pad=b"real")
    notes_media.store(a.ctx, sid, sha(data), data)
    monkeypatch.setattr(notes_media, "read", lambda *a: webp(pad=b"forged"))
    await pull(b, a)
    assert media_shas(b, sid) == {sha(data)}  # the row came with the document
    assert files(b, sid) == []  # the forged bytes were refused


async def test_a_missing_blob_is_asked_for_in_the_background_once(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    data = webp(pad=b"lost on B")
    notes_media.store(a.ctx, sid, sha(data), data)
    await pull(b, a)
    notes_media.remove_blob(b.ctx, sid, sha(data))  # B lost the file, the row stays
    assert files(b, sid) == []
    asked = []
    original = b.sync._fetch_media

    async def counting(client, session_id, digest):
        asked.append((session_id, digest))
        return await original(client, session_id, digest)

    b.sync._fetch_media = counting
    value = f"{sha(data)}.webp@640x480"
    notes_media._missing_hooks(b.ctx, sid, [value])  # what a fold does
    notes_media._missing_hooks(b.ctx, sid, [value])  # ... on every fold: one try
    await asyncio.sleep(0)
    await b.sync.drain()
    assert asked == [(sid, sha(data))]
    assert notes_media.read(b.ctx, sid, sha(data)) == data


async def test_a_blob_nobody_has_stays_missing_without_trouble(nodes):  # noqa: F811
    a, b = nodes
    sid = meeting(a)
    data = webp(pad=b"nowhere")
    notes_media.store(a.ctx, sid, sha(data), data)
    await pull(b, a)
    notes_media.remove_blob(b.ctx, sid, sha(data))
    notes_media.remove_blob(a.ctx, sid, sha(data))
    notes_media._missing_hooks(b.ctx, sid, [f"{sha(data)}.webp@640x480"])
    await asyncio.sleep(0)
    await b.sync.drain()
    assert files(b, sid) == [] and media_shas(b, sid) == {sha(data)}
