"""Guest notes through the API (api/routes/guest_notes.py) and the pages around it: the owner's
routes, the team server's rules, the open /n/ pages, the meeting's deletion and the records
export. The host is tests/fake_guest_room.py behind `ctx.http_transport`; the app is driven
through httpx.ASGITransport (the host's WebSocket needs the test's own event loop)."""

import asyncio
import io
import json
import zipfile

import httpx
import pytest

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.api.routes import guest_notes as guest_routes
from mnemosyne.api.routes import publishing as publishing_routes
from mnemosyne.models.session import Session
from mnemosyne.services import guest_format as gf
from mnemosyne.services import guest_notes, records, shared_notes, sharing
from tests import fake_link
from tests.fake_guest_room import FakeRoomHost, routed

pytestmark = pytest.mark.anyio


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)
    monkeypatch.setattr(guest_notes, "TICK", 1000.0)
    monkeypatch.setattr(guest_notes, "BACKOFF", (0.01,))
    monkeypatch.setattr(guest_notes, "RETRY_DELAY", 0.0)


@pytest.fixture
def sealed(monkeypatch):
    """records.seal_later without the background hashing: the reasons it was asked for."""
    reasons = []
    monkeypatch.setattr(records, "seal_later", lambda app, sid, reason: reasons.append(reason))
    return reasons


async def _settle(ctx):
    await guest_notes.shutdown(ctx)
    await ctx.rooms_link.stop()
    hub = ctx.notes_hub
    while hub is not None and hub.tasks:
        await asyncio.gather(*list(hub.tasks), return_exceptions=True)


@pytest.fixture
async def host(ctx, settings, sidecar):
    h = await FakeRoomHost().start()
    ctx.http_transport = h.transport()
    settings.publish_host = h.url
    settings.guest_transport = "host"  # phase 9's rooms; iroh rooms: `iroh_host` below
    settings.local_speaker_name = "Corey"
    yield h
    await _settle(ctx)
    await h.close()


def api(app, headers=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {}
    )


def meeting(ctx, **kw) -> str:
    return ctx.repo.save(Session(name="Planning", notes="launch plan\n", **kw)).id


def url(sid: str, tail: str = "") -> str:
    return f"/api/sessions/{sid}/guest-notes{tail}"


# ---- the owner ------------------------------------------------------------------------------


async def test_the_owner_starts_ends_renews_and_stops(app, ctx, host, sealed):
    sid = meeting(ctx)
    async with api(app) as c:
        r = await c.get(url(sid))
        assert r.status_code == 200 and r.json() is None  # none yet: null, not 404

        r = await c.put(url(sid))
        assert r.status_code == 200, r.text
        room = r.json()
        first, _ = host.room()
        key = ctx.repo.guest_room(sid)["key"]
        assert room["state"] == "live" and room["host_url"] == host.url
        assert room["url"] == f"{host.url}/n/{first}#{gf.key_to_fragment(key)}"
        assert room["created_at"] and room["ended_at"] == "" and room["guests_present"] == []
        assert (await c.get(url(sid))).json() == room
        assert (await c.put(url(sid))).json()["url"] == room["url"]  # the same room again
        assert len(host.rooms) == 1

        r = await c.post(url(sid, "/end"))
        assert r.status_code == 200 and r.json()["state"] == "ended" and r.json()["ended_at"]
        assert (await c.get(url(sid))).json()["state"] == "ended"
        assert host.rooms[first].ended
        assert (await c.post(url(sid, "/end"))).status_code == 404  # nothing live to end

        # Opened again after the end: a new room, the ended one deleted.
        again = (await c.put(url(sid))).json()
        assert again["state"] == "live" and again["url"] != room["url"]
        assert host.rooms[first].deleted

        second = next(k for k, v in host.rooms.items() if not v.deleted)
        renewed = (await c.post(url(sid, "/new-link"))).json()
        assert renewed["state"] == "live" and renewed["url"] != again["url"]
        assert host.rooms[second].deleted
        third, _ = host.room()
        assert third not in (first, second)

        r = await c.delete(url(sid))
        assert r.status_code == 200
        assert r.json()["state"] == "stopped" and r.json()["url"] is None
        assert host.rooms[third].deleted
        assert (await c.get(url(sid))).json() is None  # a stopped room is not shown
        assert (await c.delete(url(sid))).status_code == 404
        assert (await c.post(url(sid, "/new-link"))).status_code == 404
        assert (await c.post(url(sid, "/end"))).status_code == 404

    kinds = [e["kind"] for e in ctx.repo.events(sid) if e["kind"].startswith("guest_notes_")]
    assert "guest_notes_started" in kinds and "guest_notes_ended" in kinds
    assert kinds[-1] == "guest_notes_stopped"
    assert {"guest notes started", "guest notes ended", "guest notes stopped"} <= set(sealed)
    assert set(sealed) <= records.EVENT_SEALS  # every one is its own link in the chain
    # No key anywhere in the meeting's history.
    shown = json.dumps([e["detail"] for e in ctx.repo.events(sid)])
    assert gf.key_to_fragment(key) not in shown


async def test_unavailable_is_409_and_a_refusing_host_is_502(app, ctx, host, settings):
    sid = meeting(ctx)
    async with api(app) as c:
        settings.publish_host = ""
        r = await c.put(url(sid))
        assert r.status_code == 409 and "No host for guest notes" in r.json()["detail"]
        settings.publish_host = host.url

        host.status[("PUT", "")] = 503
        r = await c.put(url(sid))
        assert r.status_code == 502
        assert ctx.repo.guest_room(sid) is None
        host.status.clear()

        theirs = meeting(ctx)
        ctx.repo._conn.execute("UPDATE sessions SET shared_from=? WHERE id=?", ("ab" * 32, theirs))
        r = await c.put(url(theirs))
        assert r.status_code == 409 and "shared this meeting" in r.json()["detail"]

        assert (await c.put(url("nosuchmeeting"))).status_code == 404
        assert (await c.get(url("nosuchmeeting"))).status_code == 404


async def test_without_the_sidecar_it_is_409(app, ctx, settings, tmp_path):
    settings.publish_host = "https://pub.example.org"
    sid = meeting(ctx)
    async with api(app) as c:
        r = await c.put(url(sid))
    assert r.status_code == 409 and "sidecar" in r.json()["detail"]


# ---- a team server --------------------------------------------------------------------------


@pytest.fixture
async def team(settings, keystore, sidecar):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    h = await FakeRoomHost().start()
    ctx.http_transport = h.transport()
    settings.publish_host = h.url
    people = {}
    for name, role in [("Admin", "admin"), ("Ann", "member"), ("Rex", "reviewer")]:
        user = ctx.users.add(name, f"{name.lower()}@team.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"})
    yield app, ctx, people, h
    await _settle(ctx)
    await h.close()


async def test_a_team_server_needs_it_turned_on_and_readers_get_no_link(team, sealed):
    app, ctx, people, host = team
    ann, ann_auth = people["Ann"]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        sid = meeting(ctx, owner_id=ann.id)
    finally:
        access.current.reset(reset)

    async with api(app, ann_auth) as owner:
        r = await owner.put(url(sid))
        assert r.status_code == 403 and "turned off" in r.json()["detail"]
        ctx.settings.allow_guest_notes = True
        room = (await owner.put(url(sid))).json()
        assert room["url"] and room["started_by"] == "Ann"

    # A reviewer reads every meeting: she sees that guests are in, not the link or their names.
    async with api(app, people["Rex"][1]) as reader:
        seen = (await reader.get(url(sid))).json()
        assert seen["state"] == "live" and seen["url"] is None and seen["guests_present"] == []
        for call in (
            reader.put(url(sid)),
            reader.post(url(sid, "/end")),
            reader.post(url(sid, "/new-link")),
            reader.delete(url(sid)),
        ):
            assert (await call).status_code == 403
    assert not host.room()[1].deleted and not host.room()[1].ended

    # A member the meeting is not shared with does not know it exists.
    bea = ctx.users.add("Bea", "bea@team.test", "member")
    code, _ = ctx.users.invite(bea.id)
    bea_auth = {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"}
    async with api(app, bea_auth) as other:
        assert (await other.get(url(sid))).status_code == 404
        assert (await other.put(url(sid))).status_code == 404
        sharing.set_shares(ctx, sid, {bea.id}, ann.name)
        seen = (await other.get(url(sid))).json()
        assert seen["state"] == "live" and seen["url"] is None
        assert (await other.delete(url(sid))).status_code == 403

    # An admin has the link and may end it.
    async with api(app, people["Admin"][1]) as admin:
        assert (await admin.get(url(sid))).json()["url"] == room["url"]
        r = await admin.post(url(sid, "/end"))
        assert r.status_code == 200 and r.json()["state"] == "ended"
        assert r.json()["url"] == room["url"]


async def test_turning_it_off_does_not_trap_a_room_that_exists(team):
    app, ctx, people, host = team
    ctx.settings.allow_guest_notes = True
    ann, ann_auth = people["Ann"]
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        sid = meeting(ctx, owner_id=ann.id)
    finally:
        access.current.reset(reset)
    async with api(app, ann_auth) as owner:
        assert (await owner.put(url(sid))).status_code == 200
        ctx.settings.allow_guest_notes = False
        assert (await owner.post(url(sid, "/end"))).status_code == 200
        assert (await owner.delete(url(sid))).status_code == 200
    assert host.rooms[next(iter(host.rooms))].deleted


# ---- the guests' page -------------------------------------------------------------------------


@pytest.fixture
def guest_build(tmp_path, monkeypatch):
    root = tmp_path / "guest"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>guest</title>")
    (root / "assets" / "page.js").write_text("export {}")
    (root / "assets" / "notes_bg.wasm").write_bytes(b"\0asm\x01\0\0\0")
    (root / ".hidden").write_text("no")
    monkeypatch.setattr(publishing_routes, "GUEST_DIR", root)
    return root


async def test_the_guest_page_is_open_and_allows_wasm(app, ctx, settings, guest_build):
    settings.api_token = "secret"  # everything under /api needs it, /n/ does not
    async with api(app) as c:
        assert (await c.get("/api/sessions")).status_code == 401
        page = await c.get("/n/abcdefghijklmnop")
        assert page.status_code == 200 and "<title>guest</title>" in page.text
        csp = page.headers["content-security-policy"]
        assert "script-src 'self' 'wasm-unsafe-eval'" in csp
        assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"

        js = await c.get("/n/assets/page.js")
        assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
        wasm = await c.get("/n/assets/notes_bg.wasm")
        assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm"
        assert wasm.content.startswith(b"\0asm")
        assert wasm.headers["x-content-type-options"] == "nosniff"
        assert "wasm-unsafe-eval" in wasm.headers["content-security-policy"]

        assert (await c.get("/n/assets/missing.js")).status_code == 404
        assert (await c.get("/n/.hidden/x")).status_code == 404
        assert (await c.get("/n/assets/..%2f..%2findex.html")).status_code == 404
        # The publication viewer's policy did not change.
        viewer = await c.get("/p/abcdefgh")
        assert "wasm-unsafe-eval" not in viewer.headers["content-security-policy"]


async def test_the_guest_csp_names_the_relays_and_the_index_is_served(
    app, ctx, settings, guest_build
):
    settings.remote_relays = "https://relay.example.com:7443/relay, http://127.0.0.1:3340"
    async with api(app) as c:
        page = await c.get("/n/")
        assert page.status_code == 200 and "<title>guest</title>" in page.text
        # Relative asset URLs need the slash: /n is sent there.
        bare = await c.get("/n", follow_redirects=False)
        assert bare.status_code == 308 and bare.headers["location"] == "/n/"
        csp = (await c.get("/n/abcdefghijklmnop")).headers["content-security-policy"]
        for origin in (
            "https://*.relay.n0.iroh.link",
            "wss://*.relay.n0.iroh.link",
            "https://relay.petty.services",
            "wss://relay.petty.services",
            "https://relay.example.com:7443",
            "wss://relay.example.com:7443",
            "http://127.0.0.1:3340",
            "ws://127.0.0.1:3340",
        ):
            assert origin in csp
        asset = await c.get("/n/assets/page.js")
        assert "wss://relay.example.com:7443" in asset.headers["content-security-policy"]
        viewer = await c.get("/p/abcdefgh")
        assert "relay" not in viewer.headers["content-security-policy"]


async def test_the_guest_page_is_open_on_a_team_server(team, guest_build):
    app, *_ = team
    async with api(app) as c:  # no token at all
        assert (await c.get("/n/abcdefghijklmnop")).status_code == 200
        assert (await c.get("/n/assets/notes_bg.wasm")).status_code == 200
        assert (await c.get("/api/sessions/x/guest-notes")).status_code == 401


async def test_a_missing_build_says_so(app, monkeypatch, tmp_path):
    monkeypatch.setattr(publishing_routes, "GUEST_DIR", tmp_path / "nothing")
    async with api(app) as c:
        r = await c.get("/n/abcdefghijklmnop")
    assert r.status_code == 503 and "not built" in r.text
    assert "wasm-unsafe-eval" in r.headers["content-security-policy"]


# ---- deleting the meeting, lifecycle, records -------------------------------------------------


async def test_deleting_the_meeting_stops_its_room(app, ctx, host):
    sid = meeting(ctx)
    async with api(app) as c:
        await c.put(url(sid))
        room_id, room = host.room()
        r = await c.delete(f"/api/sessions/{sid}")
        assert r.status_code == 200, r.text
    assert room.deleted and guest_notes.bridge_for(ctx, sid) is None
    assert ctx.repo.get(sid) is None


async def test_a_meeting_removed_another_way_loses_its_room_too(ctx, host):
    """Sync, combining and contacts delete through the service, not the route."""
    sid = meeting(ctx)
    await guest_notes.start(ctx, sid)
    _, room = host.room()
    assert ctx.sessions.delete_session(sid)
    for _ in range(200):
        if room.deleted:
            break
        await asyncio.sleep(0.01)
    assert room.deleted


async def test_the_recording_stopping_schedules_the_end(app, ctx, host, settings):
    sid = meeting(ctx)
    settings.guest_notes_end_hours = 2
    async with api(app) as c:
        await c.put(url(sid))
    assert ctx.repo.guest_room(sid)["auto_end_at"] == ""
    guest_notes.recording_stopped(ctx, sid)
    assert ctx.repo.guest_room(sid)["auto_end_at"] != ""


async def test_the_records_export_lists_the_room_never_its_secrets(app, ctx, host, tmp_path):
    sid = meeting(ctx)
    other = meeting(ctx)
    async with api(app) as c:
        link = (await c.put(url(sid))).json()["url"]
        await c.post(url(sid, "/end"))
    row = ctx.repo.guest_room(sid)
    dest = tmp_path / "export.zip"
    records.export_zip(ctx, records.ExportRequest(session_ids=[sid, other]), dest)
    z = zipfile.ZipFile(io.BytesIO(dest.read_bytes()))
    files = [n for n in z.namelist() if n.endswith("guest_notes.json")]
    assert len(files) == 1 and f"({sid})" in files[0]  # only the meeting that had guests
    info = json.loads(z.read(files[0]))
    assert info["host"] == host.url and info["room"] == row["room"]
    assert info["created_at"] and info["ended_at"] and info["deleted_at"] is None
    assert set(info) == {
        "host",
        "room",
        "created_at",
        "started_by",
        "ended_at",
        "deleted_at",
        "expires_at",
        "transport",
        "endpoint_id",
        "relay",
        "public_url",
        "mirror_host",
    }
    assert info["transport"] == "host" and info["mirror_host"] == ""
    everything = b"".join(z.read(n) for n in z.namelist())
    for secret in (
        gf.key_to_fragment(row["key"]).encode(),
        row["key"].hex().encode(),
        row["token"].encode(),
        row["room_token"].encode(),
        link.encode(),
        row["mirror_token"].encode() or b"\0never",
    ):
        assert secret not in everything
    assert b"guest_notes.json" in z.read("SHA256SUMS")
    assert "guest_notes.json" in z.read("README.txt").decode()


# ---- iroh rooms (phase 10) ----------------------------------------------------------------

ENDPOINT = "b" * 64
N0_RELAY = "https://euw1-1.relay.n0.iroh.link./"


@pytest.fixture
async def iroh_host(ctx, settings, sidecar):
    """auto: the rooms sidecar's loopback host is one FakeRoomHost, the mirror another."""
    loop, mirror = await FakeRoomHost().start(), await FakeRoomHost().start()
    sidecar.config.update(listen=loop.url.removeprefix("http://"), endpoint_id=ENDPOINT)
    sidecar.config["relay"] = N0_RELAY
    sidecar.sync()
    ctx.http_transport = routed(loop, mirror)
    settings.guest_transport = "auto"
    settings.remote_relays = N0_RELAY  # a relay is configured (blank + ours off = no iroh)
    settings.publish_host = mirror.url
    settings.local_speaker_name = "Corey"
    yield loop, mirror
    await _settle(ctx)
    await loop.close()
    await mirror.close()


async def test_an_iroh_room_through_the_api(app, ctx, iroh_host, sealed, sidecar):
    loop, mirror = iroh_host
    sid = meeting(ctx)
    async with api(app) as c:
        r = await c.put(url(sid))
        assert r.status_code == 200, r.text
        room = r.json()
        row = ctx.repo.guest_room(sid)
        key = gf.key_to_fragment(row["key"])
        assert room["transport"] == "iroh" and room["relay"] == N0_RELAY
        assert room["relay_kind"] == "n0" and room["mirror_host_url"] == mirror.url
        assert room["page_origin"] == f"{mirror.url}/n/" and room["public_url"] == ""
        assert room["url"] == (
            f"{mirror.url}/n/{row['room']}#2.{row['room']}.{key}.{ENDPOINT}"
            ".euw1-1.relay.n0.iroh.link."
        )
        assert loop.room()[0] == mirror.room()[0] == row["room"]
        # Readers see how, not the link.
        reader = access.Principal("u9", "Rita", access.REVIEWER)
        token = access.current.set(reader)
        try:
            seen = guest_routes._model(ctx, row, ctx.repo.get(sid))
        finally:
            access.current.reset(token)
        assert seen.url is None and seen.transport == "iroh"

        r = await c.delete(url(sid))
        assert r.status_code == 200 and r.json()["state"] == "stopped"
        assert loop.rooms[row["room"]].deleted and mirror.rooms[row["room"]].deleted
    assert {"op": "room_stop", "room": row["room"]} in sidecar.ops
    assert not ctx.rooms_link.active


async def test_an_iroh_room_without_a_mirror_names_the_page_origin(
    app, ctx, iroh_host, settings, sealed
):
    settings.publish_host = ""
    settings.guest_page_url = "https://pages.example/mnemosyne/n/"
    sid = meeting(ctx)
    async with api(app) as c:
        room = (await c.put(url(sid))).json()
    assert room["transport"] == "iroh" and room["mirror_host_url"] == ""
    assert room["page_origin"] == "https://pages.example/mnemosyne/n/"
    assert room["url"].startswith("https://pages.example/mnemosyne/n/#2.")


async def test_no_iroh_and_no_host_is_409(app, ctx, iroh_host, settings, sidecar):
    settings.publish_host = ""
    sidecar.config["fail"] = {"room_start": "no relay reachable"}
    sidecar.sync()
    sid = meeting(ctx)
    async with api(app) as c:
        r = await c.put(url(sid))
    assert r.status_code == 409 and "no relay reachable" in r.json()["detail"]
    assert ctx.repo.guest_room(sid) is None
