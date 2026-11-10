"""A team member's own backend joins the team and proxies it (services/team_member.py,
api/routes/team_member.py; docs/plans/2026-11-10-team-members-in-the-app.md 1.2, M8, R1, R2).

The host is this process's `app` hosting a team (Corey the owner, Sam a member with a MEMBER
device), served by uvicorn on a free loopback port behind `Tunnelled`, so every request reaches
it as one through the link sidecar's tunnel, as the dial port's do. The member is a second app;
`joined()` writes its team.json and points `listen` at the host (no iroh, no sidecar). It is
driven over httpx.ASGITransport, and served by uvicorn too where a test needs real streaming or
a WebSocket. Joining itself (pair, dial, hang up) runs against the stand-in sidecar
(tests/fake_link.py), whose replies the test sets."""

import asyncio
import base64
import contextlib
import json
import os
import stat
import time

import httpx
import pytest
from fastapi import Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from mnemosyne.api.app import create_app
from mnemosyne.api.routes.team_member import _forwardable
from mnemosyne.config import Settings
from mnemosyne.models.session import Session
from mnemosyne.services.team_member import Membership
from mnemosyne.services.tunnel_listener import Tunnelled
from mnemosyne.storage.crypto import MemoryKeyStore
from tests import fake_link

pytestmark = pytest.mark.anyio

LOCAL = "local-token"  # the member computer's own API token: never seen by the host
HOST_EP = "b7" * 32
SAM_EP = "c4" * 32
AUDIO = bytes(range(256)) * 40


def make_ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


async def until(check, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@contextlib.asynccontextmanager
async def serve(asgi):
    """`asgi` on a free loopback port (no lifespan): its port."""
    import uvicorn

    config = uvicorn.Config(asgi, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


# ---- the host ---------------------------------------------------------------------------------


class HostSide:
    def __init__(self, app):
        self.app = app
        self.ctx = app.state.ctx
        self.seen: list[dict] = []  # what the probe routes were sent
        self.release = asyncio.Event()  # lets the stream route finish
        self.port = 0
        self.token = ""
        self.ids: dict[str, str] = {}
        self.users: dict[str, str] = {}

    def secrets_seen(self) -> str:
        return json.dumps(self.seen)


def _probe_routes(host: HostSide) -> None:
    app = host.app

    def record(request: Request, body: bytes = b"") -> dict:
        s = request.state
        seen = {
            "channel": getattr(s, "channel", None),
            "user_id": getattr(s, "user_id", None),
            "device_id": getattr(s, "device_id", None),
            "tunnel": getattr(s, "tunnel", None),
            "headers": dict(request.headers),
            "query": dict(request.query_params),
            "method": request.method,
            "body": body.decode(errors="replace"),
        }
        host.seen.append(seen)
        return seen

    async def echo(request: Request):
        return record(request, await request.body())

    async def stream(request: Request):
        record(request)

        async def chunks():
            yield b"first|"
            await host.release.wait()
            yield b"second"

        return StreamingResponse(chunks(), media_type="text/plain")

    async def ws_echo(ws: WebSocket):
        await ws.accept()
        await ws.send_json(
            {
                "channel": ws.scope["state"].get("channel"),
                "authorization": ws.headers.get("authorization"),
                "query": dict(ws.query_params),
            }
        )
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await ws.send_bytes(msg["bytes"][::-1])
            elif (msg.get("text") or "").startswith("close:"):
                await ws.close(code=int(msg["text"][6:]), reason="asked")
                return
            else:
                await ws.send_text(msg["text"].upper())

    app.add_api_route("/api/_t/echo", echo, methods=["GET", "PUT", "POST"])
    app.add_api_route("/api/_t/stream", stream)
    app.add_api_websocket_route("/ws/_t/echo", ws_echo)
    # Before api/web.py's catch-all, if any: probe routes go first.
    app.router.routes.insert(0, app.router.routes.pop())
    app.router.routes.insert(0, app.router.routes.pop())
    app.router.routes.insert(0, app.router.routes.pop())


@pytest.fixture
def settings(settings):
    settings.api_token = "admin-host"
    settings.live_transcription = False
    settings.copilot = False
    return settings


@pytest.fixture
async def host(app, ctx, tmp_path):
    """The hosting computer, served through the tunnel mark. Sam has a meeting with audio,
    Corey a personal one; Sam's app is a MEMBER device whose token is `host.token`."""
    h = HostSide(app)
    corey = ctx.users.add("Corey", "", "admin")
    sam = ctx.users.add("Sam", "", "member")
    ctx.settings.team_mode = True
    ctx.settings.team_owner_id = corey.id
    ctx.follow_hosting()
    h.users = {"Corey": corey.id, "Sam": sam.id}
    audio = tmp_path / "sam.wav"
    audio.write_bytes(AUDIO)
    h.ids["sam"] = ctx.repo.save(
        Session(name="Sam's standup", owner_id=sam.id, audio_file=str(audio))
    ).id
    h.ids["corey"] = ctx.repo.save(Session(name="Corey's own", owner_id=corey.id)).id
    _, h.token = ctx.pairing.add_member("Sam's Mac", SAM_EP, sam.id)
    _probe_routes(h)
    async with serve(Tunnelled(app)) as port:
        h.port = port
        yield h


# ---- the member -------------------------------------------------------------------------------


@pytest.fixture
def member_settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "member" / "data",
        backup_dir=str(tmp_path / "member" / "backups"),
        use_our_servers=False,
        api_token=LOCAL,
    )


@pytest.fixture
async def member(member_settings):
    app = create_app(member_settings, keystore=MemoryKeyStore())
    yield app
    await app.state.ctx.team_member.shutdown()


def joined(member, host: HostSide, token: str | None = None) -> None:
    """The test seam: what a join leaves behind, pointed at the served host."""
    tm = member.state.ctx.team_member
    tm.store.save(
        Membership(
            host_name="Corey's desktop",
            host_ticket=make_ticket(HOST_EP),
            host_endpoint=HOST_EP,
            token=token or host.token,
            user={"id": host.users["Sam"], "name": "Sam", "role": "member"},
            joined_at="2026-11-10T10:00:00+00:00",
        )
    )
    tm.listen = f"127.0.0.1:{host.port}"


def local(app, token: str | None = LOCAL, *, tunnel=False, base="http://127.0.0.1:8008"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=Tunnelled(app) if tunnel else app),
        base_url=base,
        headers=headers,
        timeout=10,
    )


# ---- forwarding HTTP --------------------------------------------------------------------------


async def test_requests_reach_the_host_as_the_member_with_the_member_token(member, host):
    joined(member, host)
    async with local(member) as c:
        r = await c.get(
            "/api/joined/api/_t/echo?x=1&token=" + LOCAL,
            headers={
                "Cookie": "session=local-cookie",
                "X-Forwarded-For": "10.0.0.9",
                "Forwarded": "for=10.0.0.9",
                "X-Kept": "yes",
            },
        )
        assert r.status_code == 200, r.text
        seen = r.json()
        assert (seen["channel"], seen["user_id"]) == ("member", host.users["Sam"])
        assert seen["tunnel"] is True
        assert seen["headers"]["authorization"] == f"Bearer {host.token}"
        assert seen["query"] == {"x": "1"}
        assert seen["headers"]["x-kept"] == "yes"
        for dropped in ("cookie", "x-forwarded-for", "forwarded"):
            assert dropped not in seen["headers"]
        # A body goes too, and a PUT.
        r = await c.put("/api/joined/api/_t/echo", json={"hello": "host"})
        assert r.json()["method"] == "PUT" and json.loads(r.json()["body"]) == {"hello": "host"}
        # The host's own routes answer as Sam: hers, not Corey's.
        me = (await c.get("/api/joined/api/users/me")).json()
        assert (me["id"], me["owner"]) == (host.users["Sam"], False)
        names = {s["name"] for s in (await c.get("/api/joined/api/sessions")).json()}
        assert "Sam's standup" in names and "Corey's own" not in names
        r = await c.put(
            f"/api/joined/api/sessions/{host.ids['sam']}/local-only", json={"local_only": True}
        )
        assert r.status_code == 200 and r.json()["local_only"] is True
        r = await c.put(
            f"/api/joined/api/sessions/{host.ids['corey']}/local-only", json={"local_only": True}
        )
        assert r.status_code == 404
        assert (await c.get("/api/joined/health")).json()["status"] == "ok"
    # R1: this computer's token never reached the host, in a header, a query or a cookie.
    assert LOCAL not in host.secrets_seen() and "local-cookie" not in host.secrets_seen()


async def test_the_query_token_alone_is_swapped_too(member, host):
    """<audio> elements authenticate with `?token=` only: still the member token upstream."""
    joined(member, host)
    async with local(member, token=None) as c:
        r = await c.get(f"/api/joined/api/_t/echo?token={LOCAL}")
    assert r.status_code == 200
    assert r.json()["headers"]["authorization"] == f"Bearer {host.token}"
    assert r.json()["query"] == {}
    assert LOCAL not in host.secrets_seen()


async def test_audio_range_requests_are_forwarded(member, host):
    joined(member, host)
    async with local(member) as c:
        r = await c.get(
            f"/api/joined/api/audio/file/{host.ids['sam']}?token={LOCAL}",
            headers={"Range": "bytes=10-19"},
        )
    assert r.status_code == 206, r.text
    assert r.headers["content-range"] == f"bytes 10-19/{len(AUDIO)}"
    assert r.content == AUDIO[10:20]


async def test_responses_and_requests_stream_without_buffering(member, host):
    """The first chunk arrives while the host has not finished its answer (an Ask answer, a
    records export), and a request body is streamed up."""
    joined(member, host)
    async with serve(member) as port, httpx.AsyncClient(timeout=10) as c:
        base = f"http://127.0.0.1:{port}/api/joined"
        auth = {"Authorization": f"Bearer {LOCAL}"}
        async with c.stream("GET", f"{base}/api/_t/stream", headers=auth) as r:
            assert r.status_code == 200
            chunks = r.aiter_raw()
            first = await asyncio.wait_for(chunks.__anext__(), 5)
            assert first == b"first|"
            host.release.set()
            rest = b"".join([chunk async for chunk in chunks])
        assert first + rest == b"first|second"

        async def upload():
            for i in range(5):
                yield f"part{i};".encode()

        r = await c.post(f"{base}/api/_t/echo", headers=auth, content=upload())
        assert r.json()["body"] == "part0;part1;part2;part3;part4;"


# ---- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "foo",
        "",
        "api",
        "apix/sessions",
        "healthz",
        "wsx",
        "api/joined",
        "api/joined/x",
        "api/joined/api/sessions",
        "api/record/abc/mic",
        "api/record",
        "api/../api/joined/x",
        "./api/sessions",
        "api/sessions/..",
        "ws/../api/joined",
        "api\\joined",
    ],
)
def test_only_the_hosts_api_sockets_and_health_are_forwardable(path):
    assert not _forwardable(path)


@pytest.mark.parametrize("path", ["api/sessions", "ws", "ws/notes/abc", "health", "api/_t/echo"])
def test_forwardable(path):
    assert _forwardable(path)


async def test_other_paths_are_never_forwarded(member, host):
    joined(member, host)
    async with local(member) as c:
        for path in (
            "/api/joined/foo",
            "/api/joined/api/joined/x",
            "/api/joined/api/joined/api/_t/echo",
            "/api/joined/api/record/abc/mic",
            "/api/joined/docs",
        ):
            assert (await c.get(path)).status_code == 404, path
            assert (await c.post(path, json={})).status_code == 404, path
    assert host.seen == []


async def test_nothing_is_forwarded_without_a_membership(member, host):
    async with local(member) as c:
        r = await c.get("/api/joined/api/_t/echo")
        assert r.status_code == 404
        assert (await c.get("/api/joined")).json()["joined"] is False
    assert host.seen == []


async def test_without_this_computers_token_nothing_is_forwarded(member, host):
    joined(member, host)
    async with local(member, token="wrong") as c:
        assert (await c.get("/api/joined/api/_t/echo")).status_code == 401
    async with local(member, token=host.token) as c:  # the member token is not a local one
        assert (await c.get("/api/joined/api/_t/echo")).status_code == 401
    assert host.seen == []


@pytest.mark.parametrize("how", ["tunnel", "team_port"])
async def test_not_through_the_tunnel_nor_on_the_team_listener(member, host, how):
    """M8: another machine (through this computer's sidecar, or the team listener) never
    drives the membership or uses the member token, whatever token it has."""
    joined(member, host)
    port = member.state.ctx.settings.team_port
    kwargs = {"tunnel": True} if how == "tunnel" else {"base": f"http://127.0.0.1:{port}"}
    async with local(member, **kwargs) as c:
        assert (await c.get("/api/joined")).status_code == 403
        assert (await c.get("/api/joined/api/_t/echo")).status_code == 403
        assert (await c.put("/api/joined/work", json={"work": "host"})).status_code == 403
        assert (await c.delete("/api/joined")).status_code == 403
        r = await c.post("/api/joined/join", json={"invite": f"{make_ticket(HOST_EP)}#abc"})
        assert r.status_code == 403
    assert host.seen == []
    assert member.state.ctx.team_member.joined


async def test_sockets_are_refused_through_the_tunnel(member, host):
    joined(member, host)
    async with serve(Tunnelled(member)) as port:
        with pytest.raises(InvalidStatus) as e:
            async with connect(f"ws://127.0.0.1:{port}/api/joined/ws?token={LOCAL}", proxy=None):
                pass
    assert e.value.response.status_code == 403


# ---- WebSockets -------------------------------------------------------------------------------


async def test_the_event_socket_is_bridged(member, host):
    joined(member, host)
    async with serve(member) as port:
        async with connect(f"ws://127.0.0.1:{port}/api/joined/ws?token={LOCAL}", proxy=None) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert hello["type"] == "hello"
            await ws.send(json.dumps({"type": "ping"}))
            assert json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"type": "pong"}


async def test_sockets_carry_text_bytes_the_token_swap_and_close_codes(member, host):
    joined(member, host)
    async with serve(member) as port:
        url = f"ws://127.0.0.1:{port}/api/joined/ws/_t/echo?a=1&token={LOCAL}"
        async with connect(url, proxy=None) as ws:
            seen = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert seen == {
                "channel": "member",
                "authorization": f"Bearer {host.token}",
                "query": {"a": "1"},
            }
            await ws.send(b"\x01\x02\x03")
            assert await asyncio.wait_for(ws.recv(), 5) == b"\x03\x02\x01"
            await ws.send("hello")
            assert await asyncio.wait_for(ws.recv(), 5) == "HELLO"
            await ws.send("close:4321")
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 4321 and ws.close_reason == "asked"


async def test_the_notes_socket_is_bridged(member, host):
    """A meeting Sam may not read is closed 4404 by the host, and that code reaches the
    window; her own answers (no sidecar here: `unavailable`)."""
    joined(member, host)
    async with serve(member) as port:
        base = f"ws://127.0.0.1:{port}/api/joined/ws/notes"
        async with connect(f"{base}/{host.ids['corey']}?token={LOCAL}", proxy=None) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 4404
        async with connect(f"{base}/{host.ids['sam']}?token={LOCAL}", proxy=None) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert hello == {"type": "hello", "unavailable": True}


# ---- signed out -------------------------------------------------------------------------------


async def test_a_refused_member_token_signs_this_computer_out(member, host):
    joined(member, host)
    tm = member.state.ctx.team_member
    host.ctx.pairing.revoke_members(host.users["Sam"])  # the owner removed Sam's app
    async with local(member) as c:
        r = await c.get("/api/joined/api/sessions")
        assert r.status_code == 409
        assert r.json()["detail"] == (
            "You were signed out of Corey's desktop; ask for a new invite"
        )
        assert tm.signed_out
        status = (await c.get("/api/joined")).json()
        assert status["joined"] and status["signed_out"] and not status["reachable"]
        before = len(host.seen)
        assert (await c.get("/api/joined/api/_t/echo")).status_code == 409
    assert len(host.seen) == before  # signed out: nothing more goes to the host
    async with serve(member) as port:
        async with connect(f"ws://127.0.0.1:{port}/api/joined/ws?token={LOCAL}", proxy=None) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 4409


async def test_a_refused_socket_signs_out_too(member, host):
    joined(member, host)
    host.ctx.users.update(host.users["Sam"], disabled=True)
    async with serve(member) as port:
        async with connect(f"ws://127.0.0.1:{port}/api/joined/ws?token={LOCAL}", proxy=None) as ws:
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
        assert ws.close_code == 4409
    assert member.state.ctx.team_member.signed_out


# ---- status, work, leave ----------------------------------------------------------------------


async def test_status_carries_the_hosts_consent_rule_and_never_the_token(member, host):
    joined(member, host)
    host.ctx.settings.require_consent = True
    async with local(member) as c:
        r = await c.get("/api/joined")
    body = r.json()
    assert body == {
        "joined": True,
        "host_name": "Corey's desktop",
        "user": {"id": host.users["Sam"], "name": "Sam", "role": "member"},
        "reachable": True,
        "work": "here",
        "consent_required": True,
        "signed_out": False,
        "pending_moves": 0,
    }
    assert host.token not in r.text


async def test_the_work_mode_is_kept(member, host):
    joined(member, host)
    async with local(member) as c:
        r = await c.put("/api/joined/work", json={"work": "host"})
        assert r.status_code == 200 and r.json()["work"] == "host"
        assert (await c.put("/api/joined/work", json={"work": "cloud"})).status_code == 422
    raw = json.loads((member.state.ctx.settings.data_dir / "team.json").read_text())
    assert raw["work"] == "host"


async def test_leaving_tells_the_host_and_forgets_the_membership(member, host):
    joined(member, host)
    path = member.state.ctx.settings.data_dir / "team.json"
    assert path.exists() and host.ctx.pairing.members_for(host.users["Sam"])
    async with local(member) as c:
        r = await c.delete("/api/joined")
        assert r.status_code == 200 and r.json()["joined"] is False
        assert (await c.delete("/api/joined")).status_code == 404
        assert (await c.get("/api/joined/api/_t/echo")).status_code == 404
    assert not path.exists()
    assert host.ctx.pairing.members_for(host.users["Sam"]) == []  # DELETE /api/pairing/me


@pytest.mark.parametrize("why", ["main", "hosting", "dedicated", "joined"])
async def test_joining_is_refused(member, host, why):
    ctx = member.state.ctx
    if why == "main":
        ctx.main_computer_store().set("http://127.0.0.1:8009", "main-token", "Desk")
    elif why == "hosting":
        ctx.settings.team_mode, ctx.settings.team_owner_id = True, "someone"
    elif why == "dedicated":
        ctx.settings.team_mode = True
    else:
        joined(member, host)
    async with local(member) as c:
        r = await c.post("/api/joined/join", json={"invite": f"{make_ticket(HOST_EP)}#abc"})
    assert r.status_code == 409, r.text
    assert why == "joined" or not ctx.team_member.joined


async def test_no_main_computer_while_in_a_team(member, host):
    """The other half of join's refusal: the shell cannot make a joined computer use a main
    computer (one or the other)."""
    joined(member, host)
    async with local(member) as c:
        body = {"url": "http://127.0.0.1:8009", "token": "main-token", "name": "Desk"}
        r = await c.put("/api/system/main", json=body)
        assert r.status_code == 409
        assert r.json()["detail"] == "This computer is in Corey's desktop: leave the team first"
        assert member.state.ctx.main_computer is None
        assert (await c.delete("/api/joined")).status_code == 200
        assert (await c.put("/api/system/main", json=body)).status_code == 200


# ---- joining through the sidecar (the stand-in) -----------------------------------------------


def _wait(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    car = fake_link.install(tmp_path, monkeypatch)
    car.config["endpoint_id"] = "a" * 64
    car.config["ticket"] = make_ticket("a" * 64)
    car.config["peers"] = {make_ticket(HOST_EP): HOST_EP}
    car.config["pair_reply"] = {
        "token": "member-token-from-host",
        "card": {
            "team": {
                "name": "Corey's desktop",
                "user": {"id": "u-sam", "name": "Sam", "role": "member"},
                "consent_required": True,
            }
        },
    }
    car.sync()
    return car


def _ops(car, op):
    return [o for o in car.ops if o["op"] == op]


def test_joining_pairs_dials_and_keeps_the_membership_privately(settings, sidecar):
    """This computer joins with an app invite: remote access and its API token are made, the
    sidecar pairs WITHOUT a way back (no `peer`) and dials the host; leaving hangs up."""
    settings.api_token = ""
    settings.team_mode = False
    app = create_app(settings, keystore=MemoryKeyStore())
    with TestClient(app) as client:
        ctx = app.state.ctx
        invite = f"{make_ticket(HOST_EP)}#code123"
        r = client.post("/api/joined/join", json={"invite": invite, "name": "Sam's Mac"})
        assert r.status_code == 200, r.text
        body = r.json()
        new_token = body["new_api_token"]
        assert new_token and ctx.settings.api_token == new_token and ctx.settings.remote_access
        assert body["joined"] and body["host_name"] == "Corey's desktop"
        assert body["user"] == {"id": "u-sam", "name": "Sam", "role": "member"}
        assert body["consent_required"] is True and body["work"] == "here"
        assert "member-token-from-host" not in r.text
        (pair,) = _ops(sidecar, "pair")
        assert pair["invite"] == invite and pair["name"] == "Sam's Mac"
        assert "peer" not in pair  # the host gets no way back in (M9)
        # Only a team membership will do: the host refuses another kind of code unused.
        assert pair["card"] == {"want": "team"}
        assert _ops(sidecar, "dial")[-1]["ticket"] == make_ticket(HOST_EP)
        tm = ctx.team_member
        assert tm.listen == "127.0.0.1:41000"
        path = ctx.settings.data_dir / "team.json"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        raw = json.loads(path.read_text())
        assert raw["token"] == "member-token-from-host" and raw["host_endpoint"] == HOST_EP
        auth = {"Authorization": f"Bearer {new_token}"}
        # A second join is refused while joined.
        assert (
            client.post("/api/joined/join", json={"invite": invite}, headers=auth).status_code
            == 409
        )

        # The sidecar restarts: the host is dialed again.
        dials = len(_ops(sidecar, "dial"))
        client.put("/api/settings", json={"remote_access": False}, headers=auth)
        client.put("/api/settings", json={"remote_access": True}, headers=auth)
        _wait(lambda: len(_ops(sidecar, "dial")) > dials)
        _wait(lambda: tm.listen is not None)

        # Leaving: the host is not there (the stand-in's port), hang up and forget anyway.
        r = client.delete("/api/joined", headers=auth)
        assert r.status_code == 200 and r.json()["joined"] is False
        assert _ops(sidecar, "hangup")[-1]["peer"] == HOST_EP
        assert not path.exists()


def test_joining_refuses_what_is_not_a_team_invite(settings, sidecar):
    settings.api_token = "s3cret"
    app = create_app(settings, keystore=MemoryKeyStore())
    auth = {"Authorization": "Bearer s3cret"}
    with TestClient(app) as client:
        ctx = app.state.ctx
        for invite in ("nonsense", "https://host:8443/?invite=abc", "endpointzzzz#abc"):
            r = client.post("/api/joined/join", json={"invite": invite}, headers=auth)
            assert r.status_code == 400, invite
        assert _ops(sidecar, "pair") == []
        # A pairing code of another kind: the reply carries no team.
        sidecar.config["pair_reply"] = {"sync": {"id": "x", "name": "Desk"}}
        sidecar.sync()
        client.put("/api/settings", json={"remote_access": False}, headers=auth)
        r = client.post(
            "/api/joined/join", json={"invite": f"{make_ticket(HOST_EP)}#abc"}, headers=auth
        )
        assert r.status_code == 400 and "not a team invite" in r.json()["detail"]
        assert not ctx.team_member.joined
        # The sidecar refuses (the host said the code is not good).
        sidecar.config["fail"] = {"pair": "This pairing code is invalid or has expired"}
        sidecar.sync()
        client.put("/api/settings", json={"remote_access": False}, headers=auth)
        r = client.post(
            "/api/joined/join", json={"invite": f"{make_ticket(HOST_EP)}#abc"}, headers=auth
        )
        assert r.status_code == 400 and "invalid or has expired" in r.json()["detail"]
        assert not ctx.team_member.joined
