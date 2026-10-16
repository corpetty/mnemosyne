"""Audio of a meeting recorded on another computer streams from it (routes/audio.py)."""

import httpx
import pytest

from mnemosyne.models.session import Session, SessionStatus
from tests.test_sync_service import nodes, pull  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio

DATA = bytes(range(256)) * 8


async def synced(a, b, tmp_path) -> str:
    audio = tmp_path / "mixed.ogg"
    audio.write_bytes(DATA)
    sid = a.repo.save(Session(name="Plan", status=SessionStatus.COMPLETED)).id
    a.ctx.sessions.set_audio(sid, str(audio), [])
    await pull(b, a)
    assert b.repo.get(sid).origin == a.id
    return sid


async def test_streams_whole_file_and_ranges(nodes, tmp_path):  # noqa: F811
    a, b = nodes
    sid = await synced(a, b, tmp_path)
    async with b.client() as c:
        full = await c.get(f"/api/audio/file/{sid}")
        assert full.status_code == 200 and full.content == DATA
        assert full.headers["content-type"].startswith("audio/ogg")
        part = await c.get(f"/api/audio/file/{sid}", headers={"Range": "bytes=10-19"})
        assert part.status_code == 206 and part.content == DATA[10:20]
        assert part.headers["content-range"] == f"bytes 10-19/{len(DATA)}"


async def test_origin_errors_pass_through(nodes, tmp_path):  # noqa: F811
    a, b = nodes
    sid = await synced(a, b, tmp_path)
    async with b.client() as c:
        r = await c.get(f"/api/audio/file/{sid}", params={"recording": "nope"})
    assert r.status_code == 404 and r.json()["detail"] == "Recording not found"


async def test_unreachable_origin_is_offline(nodes, tmp_path):  # noqa: F811
    a, b = nodes
    sid = await synced(a, b, tmp_path)

    def down(peer):
        def handler(request):
            raise httpx.ConnectError("refused")

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://peer")

    b.sync.client_for = down
    async with b.client() as c:
        r = await c.get(f"/api/audio/file/{sid}")
    assert r.status_code == 409
    assert "The audio is on A" in r.json()["detail"] and "offline" in r.json()["detail"]


async def test_unknown_peer_names_the_fallback(nodes, tmp_path):  # noqa: F811
    a, b = nodes
    sid = await synced(a, b, tmp_path)
    b.ctx.sync_peers.remove(a.id)
    async with b.client() as c:
        r = await c.get(f"/api/audio/file/{sid}")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("The audio is on") and "offline" not in r.json()["detail"]
