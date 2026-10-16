"""Pairing two computers for library sync (services/sync_peers.py, routes/sync.py, the sync
kind of routes/pairing.py) and the control requests to the link sidecar (services/link.py).

Each side is tested alone against the stand-in sidecar (tests/fake_link.py): this app is B
when it redeems an invite through /api/sync/pair, and A when a sync code is redeemed on it."""

import asyncio
import base64
import logging
import stat
import sys
import time

import pytest

from mnemosyne.services import sync_peers
from mnemosyne.services.link import LinkError, LinkService, endpoint_id_from_ticket
from mnemosyne.services.sync_peers import SyncPeer, SyncPeerStore
from tests import fake_link

ADMIN = {"Authorization": "Bearer s3cret"}
ME, OTHER, THIRD = "a" * 64, "b" * 64, "c" * 64


def make_ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


def wait_for(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    car = fake_link.install(tmp_path, monkeypatch)
    car.config["endpoint_id"] = ME
    car.config["ticket"] = make_ticket(ME)
    car.sync()
    return car


@pytest.fixture
def node(client, ctx, sidecar):
    """This app with an API token and remote access running on the stand-in sidecar."""
    ctx.settings.api_token = "s3cret"
    assert (
        client.put("/api/settings", json={"remote_access": True}, headers=ADMIN).status_code == 200
    )
    wait_for(lambda: ctx.link.status.running)
    # No background pulls (services/sync_service.py): the peers here are the stand-in's
    # loopback addresses with nothing behind them.
    client.portal.call(ctx.sync.stop)
    return client


def pair_as_b(client, other=OTHER, name="Laptop", code="code123"):
    return client.post(
        "/api/sync/pair",
        json={"invite": f"{make_ticket(other)}#{code}", "name": name},
        headers=ADMIN,
    )


def redeem_as_a(client, ctx, endpoint=THIRD, peer_id="laptop123456", token="token-for-a"):
    code = client.post("/api/pairing/codes", json={"kind": "sync"}, headers=ADMIN).json()
    peer = {"id": peer_id, "name": "Laptop", "ticket": make_ticket(endpoint), "token": token}
    return code, client.post(
        "/api/pairing/redeem",
        json={"code": code["code"], "name": "Laptop", "endpoint_id": endpoint, "peer": peer},
    )


def ops(sidecar, op):
    return [o for o in sidecar.ops if o["op"] == op]


def test_ticket_names_its_endpoint():
    assert endpoint_id_from_ticket(make_ticket(OTHER)) == OTHER
    real = (
        "endpointacit4fy22ies5fgkzn3ff75oxp55mfdvghc32rz37qj5brpwsf2smbaaenuhi5dqom5c6l3vonstcl"
        "jrfzzgk3dbpexg4mbonfzg62bonruw42zof4aqazdfizikjiycaeak2kts2kskgaqbadakqameusrqe"
    )
    assert endpoint_id_from_ticket(real) == (
        "913e171ad2092e94cacb7652ffaebbfbd6147531c5bd473bfc13d0c5f6917526"
    )
    for bad in ("", "nope", "endpoint!!!", "endpointaaaa"):
        with pytest.raises(ValueError):
            endpoint_id_from_ticket(bad)


def test_store_keeps_identity_and_peers_privately(tmp_path):
    path = tmp_path / "sync_peers.json"
    store = SyncPeerStore(path)
    me = store.self_info()
    assert len(me.id) == 12 and int(me.id, 16) >= 0 and me.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    peer = SyncPeer("p1", "Desk", OTHER, "tk", "secret", "2026-10-14T00:00:00+00:00")
    store.add(peer)
    store.update("p1", listen="127.0.0.1:1", last_sync_at="2026-10-14T01:00:00+00:00")
    again = SyncPeerStore(path)
    assert again.self_info() == me
    assert again.get("p1").listen == "127.0.0.1:1" and again.get("p1").token == "secret"
    assert again.find_by_endpoint(OTHER).id == "p1" and again.find_by_endpoint(THIRD) is None
    # The same machine paired again replaces its record.
    again.add(SyncPeer("p2", "Desk", OTHER, "tk2", "s2", "2026-10-14T02:00:00+00:00"))
    assert [p.id for p in again.peers()] == ["p2"]
    assert again.remove("p2") is not None and again.remove("p2") is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pairing_as_b_sends_pair_and_records_a(node, ctx, sidecar):
    r = pair_as_b(node)
    assert r.status_code == 200, r.text
    peer = r.json()["peer"]
    assert (peer["id"], peer["name"], peer["endpoint_id"]) == ("peerpeerpeer", "Desk", OTHER)
    assert peer["listen"] == "127.0.0.1:41000"

    pair_op = ops(sidecar, "pair")[0]
    me = ctx.sync_peers.self_info()
    assert pair_op["invite"] == f"{make_ticket(OTHER)}#code123" and pair_op["name"] == "Laptop"
    sent = pair_op["peer"]
    assert (sent["id"], sent["name"], sent["ticket"]) == (me.id, "Laptop", make_ticket(ME))
    # The token handed to A opens this computer as a DESKTOP device bound to A's endpoint.
    device = ctx.pairing.verify(sent["token"])
    assert device is not None and device.kind == "desktop" and device.endpoint_id == OTHER
    # B then dials A and keeps the token A gave it.
    assert ops(sidecar, "dial")[0]["ticket"] == make_ticket(OTHER)
    stored = ctx.sync_peers.get("peerpeerpeer")
    assert stored.token == "token-from-peer" and stored.ticket == make_ticket(OTHER)
    assert stored.listen == "127.0.0.1:41000"
    assert ctx.sync_peers.self_info().name == "Laptop"


def test_pairing_as_b_asks_a_when_the_sidecar_does_not_say_who_it_is(
    node, ctx, sidecar, monkeypatch
):
    sidecar.config["pair_reply"] = {}
    sidecar.sync()
    node.put("/api/settings", json={"remote_access": False}, headers=ADMIN)
    node.put("/api/settings", json={"remote_access": True}, headers=ADMIN)
    wait_for(lambda: ctx.link.status.running)
    seen = []

    async def fetch(listen, token):
        seen.append((listen, token))
        return {"id": "fetchedfetch", "name": "Office"}

    monkeypatch.setattr(sync_peers, "fetch_peer_self", fetch)
    r = pair_as_b(node)
    assert r.status_code == 200 and r.json()["peer"]["id"] == "fetchedfetch"
    assert seen == [("127.0.0.1:41000", "token-from-peer")]
    assert r.json()["peer"]["name"] == "Office"


def test_pairing_as_b_without_an_identity_keeps_a_stand_in_id(node, ctx, sidecar, monkeypatch):
    sidecar.config["pair_reply"] = {}
    sidecar.sync()
    node.put("/api/settings", json={"remote_access": False}, headers=ADMIN)
    node.put("/api/settings", json={"remote_access": True}, headers=ADMIN)
    wait_for(lambda: ctx.link.status.running)

    async def fetch(listen, token):
        raise OSError("unreachable")

    monkeypatch.setattr(sync_peers, "fetch_peer_self", fetch)
    r = pair_as_b(node)
    assert r.status_code == 200 and r.json()["peer"]["id"] == OTHER[:12]


def test_a_refused_pairing_leaves_nothing_behind(node, ctx, sidecar):
    sidecar.config["fail"] = {"pair": "This pairing code is invalid or has expired"}
    sidecar.sync()
    node.put("/api/settings", json={"remote_access": False}, headers=ADMIN)
    node.put("/api/settings", json={"remote_access": True}, headers=ADMIN)
    wait_for(lambda: ctx.link.status.running)
    r = pair_as_b(node)
    assert r.status_code == 400 and "invalid or has expired" in r.json()["detail"]
    assert ctx.pairing.devices() == [] and ctx.sync_peers.peers() == []


def test_pairing_as_b_refusals(client, ctx, sidecar):
    body = {"invite": f"{make_ticket(OTHER)}#code", "name": "Laptop"}
    # Remote access off: the sidecar is not running.
    ctx.settings.api_token = "s3cret"
    r = client.post("/api/sync/pair", json=body, headers=ADMIN)
    assert r.status_code == 400 and "remote access" in r.json()["detail"]

    client.put("/api/settings", json={"remote_access": True}, headers=ADMIN)
    wait_for(lambda: ctx.link.status.running)
    for invite in ("nonsense", f"{make_ticket(OTHER)}#", "endpointzzz#code"):
        r = client.post("/api/sync/pair", json={"invite": invite}, headers=ADMIN)
        assert r.status_code == 400 and "sync invite" in r.json()["detail"]
    # The invite is this computer's own.
    r = client.post("/api/sync/pair", json={"invite": f"{make_ticket(ME)}#code"}, headers=ADMIN)
    assert r.status_code == 400 and "this computer" in r.json()["detail"]
    # No API token.
    ctx.settings.api_token = ""
    with pytest.raises(sync_peers.SyncPairingError, match="API token"):
        sync_peers.check_can_pair(ctx)
    # Team server.
    ctx.settings.api_token = "s3cret"
    ctx.settings.team_mode = True
    with pytest.raises(sync_peers.SyncPairingError, match="team server"):
        sync_peers.check_can_pair(ctx)
    ctx.settings.team_mode = False
    assert ops(sidecar, "pair") == [] and ctx.pairing.devices() == []


def test_sync_code_redeemed_on_a_records_b_and_answers_with_a_self(node, ctx, sidecar):
    code, r = redeem_as_a(node, ctx)
    assert code["kind"] == "sync" and code["invite"] == f"{make_ticket(ME)}#{code['code']}"
    assert r.status_code == 200, r.text
    body = r.json()
    me = ctx.sync_peers.self_info()
    assert body["sync"] == {"id": me.id, "name": me.name}
    assert body["device"]["kind"] == "desktop" and body["token"]
    assert ctx.pairing.verify(body["token"]).endpoint_id == THIRD

    stored = ctx.sync_peers.get("laptop123456")
    assert (stored.name, stored.endpoint_id, stored.token) == ("Laptop", THIRD, "token-for-a")
    assert stored.ticket == make_ticket(THIRD) and stored.listen == "127.0.0.1:41000"
    assert ops(sidecar, "dial")[0]["ticket"] == make_ticket(THIRD)
    # A code is used once.
    again = node.post("/api/pairing/redeem", json={"code": code["code"], "endpoint_id": THIRD})
    assert again.status_code == 403


def test_sync_redeem_needs_the_peers_details_and_a_personal_computer(node, ctx):
    code = node.post("/api/pairing/codes", json={"kind": "sync"}, headers=ADMIN).json()["code"]
    no_peer = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": THIRD})
    assert no_peer.status_code == 400
    half = {"id": "x", "ticket": "", "token": "t"}
    r = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": THIRD, "peer": half})
    assert r.status_code == 400
    own = {"id": "x", "name": "n", "ticket": "t", "token": "t"}
    r = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": ME, "peer": own})
    assert r.status_code == 400
    ctx.settings.team_mode = True
    r = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": THIRD, "peer": own})
    assert r.status_code == 403 and "team server" in r.json()["detail"]
    ctx.settings.team_mode = False
    # None of that used the code up.
    r = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": THIRD, "peer": own})
    assert r.status_code == 200
    assert ctx.sync_peers.get("x") is not None


def test_a_desktop_code_is_not_a_sync_code(node, ctx):
    code = node.post("/api/pairing/codes", json={"kind": "desktop"}, headers=ADMIN).json()["code"]
    peer = {"id": "x", "name": "n", "ticket": "t", "token": "t"}
    r = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": THIRD, "peer": peer})
    assert r.status_code == 200 and r.json()["sync"] is None
    assert ctx.sync_peers.peers() == []


def test_sync_codes_in_team_mode_and_without_remote_access(client, ctx):
    ctx.settings.api_token = "s3cret"
    r = client.post("/api/pairing/codes", json={"kind": "sync"}, headers=ADMIN)
    assert r.status_code == 409 and "remote access" in r.json()["detail"]


def test_removing_the_peer_removes_its_device_and_hangs_up(node, ctx, sidecar):
    pair_as_b(node)
    assert len(ctx.pairing.devices()) == 1
    r = node.delete("/api/sync/peers/peerpeerpeer", headers=ADMIN)
    assert r.status_code == 200
    assert ctx.sync_peers.peers() == [] and ctx.pairing.devices() == []
    assert ops(sidecar, "hangup") == [{"op": "hangup", "peer": OTHER}]
    assert node.delete("/api/sync/peers/peerpeerpeer", headers=ADMIN).status_code == 404
    assert node.get("/api/sync/peers", headers=ADMIN).json()["peers"] == []


def test_removing_the_device_removes_the_peer(node, ctx, sidecar):
    _, r = redeem_as_a(node, ctx)
    device_id = r.json()["device"]["id"]
    assert ctx.sync_peers.get("laptop123456") is not None
    assert node.delete(f"/api/pairing/devices/{device_id}", headers=ADMIN).status_code == 200
    assert ctx.sync_peers.peers() == []
    assert ops(sidecar, "hangup") == [{"op": "hangup", "peer": THIRD}]
    # An ordinary desktop device (no peer) is removed as before.
    code = node.post("/api/pairing/codes", json={"kind": "desktop"}, headers=ADMIN).json()["code"]
    plain = node.post("/api/pairing/redeem", json={"code": code, "endpoint_id": OTHER}).json()
    assert node.delete(f"/api/pairing/devices/{plain['device']['id']}", headers=ADMIN).status_code
    assert len(ops(sidecar, "hangup")) == 1


def test_listing_never_shows_tokens(node, ctx, sidecar):
    redeem_as_a(node, ctx, token="very-secret-token")
    peers = node.get("/api/sync/peers", headers=ADMIN)
    me = ctx.sync_peers.self_info()
    assert peers.json()["self"] == {"id": me.id, "name": me.name}
    [peer] = peers.json()["peers"]
    assert set(peer) == {"id", "name", "endpoint_id", "created_at", "last_sync_at", "listen"}
    status = node.get("/api/sync/status", headers=ADMIN)
    [row] = status.json()["peers"]
    assert {k: row[k] for k in ("id", "name", "endpoint", "listen", "last_sync_at")} == {
        "id": "laptop123456",
        "name": "Laptop",
        "endpoint": THIRD[:10],
        "listen": "127.0.0.1:41000",
        "last_sync_at": None,
    }
    for text in (peers.text, status.text, node.get("/api/pairing/devices", headers=ADMIN).text):
        assert "very-secret-token" not in text and "token-from-peer" not in text
    assert node.get("/api/sync/peers").status_code == 401


def test_peers_are_dialed_again_when_the_sidecar_restarts(node, ctx, sidecar):
    redeem_as_a(node, ctx)
    node.put("/api/settings", json={"remote_access": False}, headers=ADMIN)
    node.put("/api/settings", json={"remote_access": True}, headers=ADMIN)
    wait_for(lambda: len(ops(sidecar, "dial")) == 2)
    wait_for(lambda: ctx.sync_peers.get("laptop123456").listen == "127.0.0.1:41000")
    assert ops(sidecar, "dial")[1]["ticket"] == make_ticket(THIRD)


def test_ensure_dialed_dials_lazily(node, ctx, sidecar):
    ctx.sync_peers.add(
        SyncPeer("p9", "Desk", OTHER, make_ticket(OTHER), "t", "2026-10-14T00:00:00")
    )
    peer = ctx.sync_peers.get("p9")

    async def go():
        return await sync_peers.ensure_dialed(ctx.link, ctx.sync_peers, peer)

    r = node.portal.call(go)  # on the app's own event loop, where the sidecar runs
    assert r == "127.0.0.1:41000" and ctx.sync_peers.get("p9").listen == r
    assert node.portal.call(go) == r and len(ops(sidecar, "dial")) == 1


# ---- LinkService.request ---------------------------------------------------------------------


def script(tmp_path, body: str) -> str:
    path = tmp_path / "mnemosyne-link"
    path.write_text(f"#!{sys.executable}\nimport json, sys, time\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


READY = 'print(json.dumps({"endpoint_id": "e1d", "ticket": "endpointe1d"}), flush=True)\n'
ECHO = (
    READY
    + "for line in sys.stdin:\n"
    + "    r = json.loads(line)\n"
    + '    print("not json", flush=True)\n'
    + '    print(json.dumps({"id": "nobody", "ok": True}), flush=True)\n'
    + '    if r["op"] == "slow": time.sleep(60)\n'
    + '    ok = r["op"] != "bad"\n'
    + '    out = {"id": r["id"], "op": r["op"], "ok": ok, "error": "no way", "n": r.get("n")}\n'
    + "    print(json.dumps(out), flush=True)\n"
)


async def running(link: LinkService) -> None:
    link.start()
    for _ in range(100):
        if link.status.running:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never ran")


@pytest.mark.anyio
async def test_request_fails_fast_when_not_running(tmp_path):
    link = LinkService(tmp_path, 8008, binary=script(tmp_path, ECHO))
    with pytest.raises(LinkError, match="not running"):
        await link.request("dial", ticket="t")


@pytest.mark.anyio
async def test_request_matches_replies_by_id_and_raises_on_refusal(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    link = LinkService(tmp_path, 8008, binary=script(tmp_path, ECHO))
    await running(link)
    try:
        a, b = await asyncio.gather(link.request("dial", n=1), link.request("dial", n=2))
        assert (a["n"], b["n"]) == (1, 2) and a["op"] == "dial"
        with pytest.raises(LinkError, match="no way"):
            await link.request("bad")
        assert "unexpected output: not json" in caplog.text
    finally:
        await link.stop()
    with pytest.raises(LinkError):
        await link.request("dial")


@pytest.mark.anyio
async def test_request_times_out_and_fails_when_the_sidecar_dies(tmp_path, monkeypatch):
    monkeypatch.setattr("mnemosyne.services.link.REQUEST_TIMEOUT", 0.3)
    monkeypatch.setattr("mnemosyne.services.link.RESTART_DELAY", 60)
    link = LinkService(tmp_path, 8008, binary=script(tmp_path, ECHO))
    await running(link)
    try:
        with pytest.raises(LinkError, match="in time"):
            await link.request("slow")
    finally:
        await link.stop()

    monkeypatch.setattr("mnemosyne.services.link.REQUEST_TIMEOUT", 30.0)
    dies = READY + "sys.stdin.readline()\nsys.exit(2)\n"
    link = LinkService(tmp_path, 8008, binary=script(tmp_path, dies))
    await running(link)
    try:
        with pytest.raises(LinkError, match="stopped"):
            await link.request("dial")
    finally:
        await link.stop()
