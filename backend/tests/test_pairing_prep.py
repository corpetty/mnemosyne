"""Pairing makes its own prerequisites (services/pairing_prep.py): the API token and, for a
computer, library sync or a contact, remote access with its sidecar running. A phone needs the
token only. The list of what is connected (routes/connections.py) and the computer's role."""

import base64
import sys

import httpx
import pytest

from mnemosyne.api.app import create_app
from mnemosyne.config import Settings
from mnemosyne.services import pairing_prep
from mnemosyne.services.sync_peers import SyncPeer
from mnemosyne.storage.crypto import MemoryKeyStore
from tests import fake_link

pytestmark = pytest.mark.anyio

ME, OTHER = "a" * 64, "b" * 64


def make_ticket(endpoint_id: str) -> str:
    raw = b"\x00" + bytes.fromhex(endpoint_id) + b"\x00"
    return "endpoint" + base64.b32encode(raw).decode().lower().rstrip("=")


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    car = fake_link.install(tmp_path, monkeypatch)
    car.config["endpoint_id"] = ME
    car.config["ticket"] = make_ticket(ME)
    car.sync()
    return car


def fresh(ctx):
    assert ctx.settings.api_token == "" and not ctx.settings.remote_access
    assert not ctx.settings.team_mode


def saved(tmp_path) -> dict:
    import tomllib

    return tomllib.loads((tmp_path / "config.toml").read_text())


# ---- the code routes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body", "kind"),
    [
        ("/api/pairing/codes", {"kind": "desktop"}, "desktop"),
        ("/api/pairing/codes", {"kind": "sync"}, "sync"),
        ("/api/contacts/codes", None, "contact"),
    ],
)
def test_a_fresh_app_makes_the_token_and_remote_access(
    client, ctx, sidecar, tmp_path, path, body, kind
):
    fresh(ctx)
    r = client.post(path, json=body) if body else client.post(path)
    assert r.status_code == 200, r.text
    assert (
        r.json()["kind"] == kind and r.json()["invite"] == f"{make_ticket(ME)}#{r.json()['code']}"
    )
    assert len(ctx.settings.api_token) >= 32 and ctx.settings.remote_access
    assert ctx.link.status.running  # started, and waited for
    on_disk = saved(tmp_path)
    assert on_disk["api_token"] == ctx.settings.api_token and on_disk["remote_access"] is True
    # The API is closed from now on: the caller has to carry the token (see the window test).
    assert client.get("/api/sessions").status_code == 401
    token = {"Authorization": f"Bearer {ctx.settings.api_token}"}
    assert client.get("/api/sessions", headers=token).status_code == 200


def test_a_phone_code_makes_only_the_token(client, ctx, sidecar):
    fresh(ctx)
    r = client.post("/api/pairing/codes", json={"kind": "phone"})
    assert r.status_code == 200 and r.json()["invite"] is None
    assert ctx.settings.api_token and not ctx.settings.remote_access
    assert not ctx.link.active  # no sidecar for a phone


def test_what_is_already_set_is_left_alone(client, ctx, sidecar):
    ctx.settings.api_token = "mine"
    headers = {"Authorization": "Bearer mine"}
    assert client.post("/api/pairing/codes", json={"kind": "desktop"}, headers=headers).status_code
    assert ctx.settings.api_token == "mine" and ctx.settings.remote_access
    ops = len(sidecar.ops)
    again = client.post("/api/pairing/prepare", json={"kind": "desktop"}, headers=headers)
    assert again.status_code == 200 and again.json()["new_api_token"] is None
    assert ctx.settings.api_token == "mine" and len(sidecar.ops) == ops


def test_pairing_as_b_makes_them_too(client, ctx, sidecar):
    fresh(ctx)
    r = client.post("/api/sync/pair", json={"invite": f"{make_ticket(OTHER)}#code", "name": "Desk"})
    assert r.status_code == 200, r.text
    client.portal.call(ctx.sync.stop)  # no background pulls from the stand-in's peer
    assert ctx.settings.api_token and ctx.settings.remote_access


# ---- when it cannot be done -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/pairing/codes", {"kind": "desktop"}),
        ("/api/pairing/codes", {"kind": "sync"}),
        ("/api/contacts/codes", None),
        ("/api/pairing/prepare", {"kind": "desktop"}),
        ("/api/sync/pair", {"invite": f"{make_ticket(OTHER)}#code"}),
        ("/api/contacts/pair", {"invite": f"{make_ticket(OTHER)}#code"}),
    ],
)
def test_no_sidecar_is_a_409_with_the_reason_and_nothing_changed(client, ctx, tmp_path, path, body):
    fresh(ctx)
    r = client.post(path, json=body) if body else client.post(path)
    assert r.status_code == 409, r.text
    assert "mnemosyne-link" in r.json()["detail"]
    assert ctx.settings.api_token == "" and not ctx.settings.remote_access
    assert not (tmp_path / "config.toml").exists()
    # And the window is not locked out of its own backend by it.
    assert client.get("/api/sessions").status_code == 200


def test_a_sidecar_that_does_not_start_is_put_back(client, ctx, tmp_path, monkeypatch):
    script = tmp_path / "broken-link"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    script.chmod(0o755)
    monkeypatch.setenv("MNEMOSYNE_LINK_BIN", str(script))
    monkeypatch.setattr(pairing_prep, "PREPARE_TIMEOUT", 1.0)
    fresh(ctx)
    r = client.post("/api/pairing/codes", json={"kind": "desktop"})
    assert r.status_code == 409 and "before it was ready" in r.json()["detail"]
    assert ctx.settings.api_token == "" and not ctx.settings.remote_access
    assert not ctx.link.active  # stopped again
    assert saved(tmp_path)["api_token"] == "" if (tmp_path / "config.toml").exists() else True


def test_a_team_server_still_refuses_sync_and_contacts(client, ctx, sidecar):
    ctx.settings.team_mode = True
    ctx.settings.api_token = "s3cret"  # a team server needs a token for every request
    h = {"Authorization": "Bearer s3cret"}
    for path, body in (
        ("/api/pairing/codes", {"kind": "sync"}),
        ("/api/pairing/prepare", {"kind": "contact"}),
        ("/api/contacts/codes", None),
    ):
        r = client.post(path, json=body, headers=h) if body else client.post(path, headers=h)
        assert r.status_code == 409 and "team server" in r.json()["detail"], path
    invite = {"invite": f"{make_ticket(OTHER)}#code"}
    r = client.post("/api/sync/pair", json=invite, headers=h)
    assert r.status_code == 400 and "team server" in r.json()["detail"]
    r = client.post("/api/contacts/pair", json=invite, headers=h)
    assert r.status_code == 409 and "team server" in r.json()["detail"]
    assert not ctx.settings.remote_access and not ctx.link.active


# ---- the local window -----------------------------------------------------------------------


def loopback(app, token: str | None = None) -> httpx.AsyncClient:
    """The desktop window: 127.0.0.1, the backend's own Host, no proxy headers."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url="http://127.0.0.1:8008",
        headers=headers,
    )


async def test_the_local_window_keeps_working_after_a_token_is_made(tmp_path, monkeypatch):
    """No token set: the window sends none. The prepare call answers with the token it made, once;
    the window carries it from then on (src/lib/api/backend.ts), and everything still answers."""
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    app = create_app(
        Settings(data_dir=tmp_path / "data", backup_dir=str(tmp_path / "b"), use_our_servers=False),
        keystore=MemoryKeyStore(),
    )
    ctx = app.state.ctx
    try:
        async with loopback(app) as window:
            assert (await window.get("/api/sessions")).status_code == 200
            r = await window.post("/api/pairing/prepare", json={"kind": "phone"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["api_token_set"] and not body["remote_access"]
            token = body["new_api_token"]
            assert token == ctx.settings.api_token
            # Without it the window would now be refused, which is why it must take the token.
            assert (await window.get("/api/sessions")).status_code == 401
            # The token is handed out once.
            again = await loopback(app, token).post("/api/pairing/prepare")
            assert again.json()["new_api_token"] is None
        async with loopback(app, token) as window:
            assert (await window.get("/api/sessions")).status_code == 200
            assert (await window.get("/api/settings")).status_code == 200
            assert (await window.post("/api/pairing/codes")).status_code == 200
    finally:
        await ctx.jobs.shutdown()
        ctx.repo.close()


async def test_a_team_servers_desktop_owner_needs_no_token_and_is_not_handed_one(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            backup_dir=str(tmp_path / "b"),
            use_our_servers=False,
            team_mode=True,
        ),
        keystore=MemoryKeyStore(),
    )
    ctx = app.state.ctx
    try:
        user = ctx.users.add("Owner", "owner@example.com", "admin")
        ctx.settings.team_owner_id = user.id
        async with loopback(app) as window:
            r = await window.post("/api/pairing/prepare", json={"kind": "phone"})
            assert r.status_code == 200, r.text
            assert r.json()["new_api_token"] is None  # it runs as the owner, tokenless
            assert (await window.get("/api/sessions")).status_code == 200
    finally:
        await ctx.jobs.shutdown()
        ctx.repo.close()


# ---- the device list and the role -----------------------------------------------------------


def test_the_role(client, ctx):
    assert client.get("/api/system/role").json() == {
        "role": "main",
        "hosts_team": False,
        "personal": True,
    }
    ctx.settings.team_mode = True
    ctx.settings.api_token = "s3cret"
    r = client.get("/api/system/role", headers={"Authorization": "Bearer s3cret"})
    assert r.json() == {"role": "team", "hosts_team": False, "personal": False}


def test_connected_devices_lists_each_kind_and_a_sync_peer_once(client, ctx, tmp_path):
    from mnemosyne.services import contacts
    from mnemosyne.services.person import PersonKeys
    from tests.test_contacts import card_of

    pairing = ctx.pairing
    code, _ = pairing.new_code("phone")
    phone, _ = pairing.redeem(code, "Pixel")
    code, _ = pairing.new_code("desktop")
    laptop, _ = pairing.redeem(code, "Laptop", "c" * 64)
    # A sync peer is also a DESKTOP device: one row, as sync.
    code, _ = pairing.new_code("sync")
    pairing.redeem(code, "Desk 2", OTHER)
    ctx.sync_peers.add(
        SyncPeer("peer-1", "Desk 2", OTHER, make_ticket(OTHER), "tok", "2026-11-01T00:00:00+00:00")
    )
    ada = PersonKeys(tmp_path / "ada")
    ctx.link.status.running, ctx.link.status.ticket = True, make_ticket(ME)
    contacts.upsert_contact(ctx, card_of(ada, "Ada", machines=(("Desk", "d" * 64),)), "ab" * 32)

    rows = client.get("/api/connections/devices").json()
    assert sorted(r["kind"] for r in rows) == ["computer", "contact", "phone", "sync"]
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["phone"]["id"] == phone.id and by_kind["phone"]["can"] == "Sends recordings"
    assert by_kind["phone"]["remove"] == f"/api/pairing/devices/{phone.id}"
    assert by_kind["computer"]["id"] == laptop.id
    assert by_kind["computer"]["can"] == "Uses this computer"
    assert by_kind["sync"]["id"] == "peer-1" and by_kind["sync"]["name"] == "Desk 2"
    assert by_kind["sync"]["remove"] == "/api/sync/peers/peer-1"
    assert by_kind["sync"]["can"] == "Keeps its own library in sync"
    assert by_kind["contact"]["name"] == "Ada" and by_kind["contact"]["can"] == "Shared meetings"
    assert by_kind["contact"]["id"] == ada.public_hex
    assert by_kind["contact"]["remove"] == f"/api/contacts/{ada.public_hex}"
    keys = {"id", "kind", "name", "created_at", "last_seen_at", "can", "remove"}
    assert all(set(r) == keys for r in rows)
    # Oldest first.
    assert [r["created_at"] for r in rows] == sorted(r["created_at"] for r in rows)
    # Each remove path works.
    for r in rows:
        assert client.delete(r["remove"]).status_code == 200, r
    assert client.get("/api/connections/devices").json() == []


def test_connected_devices_is_for_the_admin(client, ctx):
    ctx.settings.api_token = "s3cret"
    assert client.get("/api/connections/devices").status_code == 401
    ok = client.get("/api/connections/devices", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
