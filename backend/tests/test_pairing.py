"""Pairing devices: phones in server mode, computers through remote access. One-time codes,
per-device tokens, removal."""

import stat

import pytest

from mnemosyne.services.pairing import (
    CODE_TTL_SECONDS,
    InvalidPairingCode,
    PairingService,
)

ADMIN = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def server(client, ctx, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("MNEMOSYNE_BIND_PORT", "8008")
    monkeypatch.setattr("mnemosyne.api.routes.mobile.lan_addresses", lambda: ["192.168.1.20"])
    ctx.settings.api_token = "s3cret"
    return client


def pair(client, name="iPhone") -> tuple[dict, str]:
    code = client.post("/api/pairing/codes", headers=ADMIN).json()
    r = client.post("/api/pairing/redeem", json={"code": code["code"], "name": name})
    assert r.status_code == 200
    body = r.json()
    return body["device"], body["token"]


def test_codes_need_an_api_token(client):
    r = client.post("/api/pairing/codes")
    assert r.status_code == 409 and "API token" in r.json()["detail"]


def test_code_links_to_the_phone_page(server):
    body = server.post("/api/pairing/codes", headers=ADMIN).json()
    assert body["urls"] == [f"http://192.168.1.20:8008/m?pair={body['code']}"]
    # Creating codes and listing devices is for the API token only.
    assert server.post("/api/pairing/codes").status_code == 401


def test_phone_url_comes_first(server, ctx):
    ctx.settings.phone_url = "https://desk.tail1234.ts.net/"
    body = server.post("/api/pairing/codes", headers=ADMIN).json()
    assert body["urls"][0] == f"https://desk.tail1234.ts.net/m?pair={body['code']}"


def test_device_token_opens_only_the_phone_paths(server):
    device, token = pair(server)
    me = server.get("/api/pairing/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["id"] == device["id"]
    assert me.json()["last_seen_at"] is not None
    phone = {"Authorization": f"Bearer {token}"}
    assert server.get("/api/sessions", headers=phone).status_code == 401
    assert server.get("/api/pairing/devices", headers=phone).status_code == 401
    assert server.post("/api/pairing/codes", headers=phone).status_code == 401
    # The import route accepts the token (400: no file sent, but past auth).
    assert server.post("/api/audio/import", headers=phone).status_code != 401


def test_code_works_once(server):
    code = server.post("/api/pairing/codes", headers=ADMIN).json()["code"]
    assert server.post("/api/pairing/redeem", json={"code": code}).status_code == 200
    again = server.post("/api/pairing/redeem", json={"code": code})
    assert again.status_code == 403 and "expired" in again.json()["detail"]
    assert server.post("/api/pairing/redeem", json={"code": "guess"}).status_code == 403


def test_removed_device_is_cut_off(server):
    device, token = pair(server)
    listed = server.get("/api/pairing/devices", headers=ADMIN).json()
    assert [d["name"] for d in listed] == ["iPhone"]
    assert server.delete(f"/api/pairing/devices/{device['id']}", headers=ADMIN).status_code == 200
    me = server.get("/api/pairing/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401
    assert server.delete(f"/api/pairing/devices/{device['id']}", headers=ADMIN).status_code == 404


def test_api_token_is_not_a_device(server):
    assert server.get("/api/pairing/me", headers=ADMIN).status_code == 404


def test_phone_page_is_open(server):
    assert server.get("/m").status_code == 200


def test_codes_expire(tmp_path):
    now = [1000.0]
    svc = PairingService(tmp_path / "paired_devices.json", clock=lambda: now[0])
    code, _ = svc.new_code()
    now[0] += CODE_TTL_SECONDS + 1
    with pytest.raises(InvalidPairingCode):
        svc.redeem(code, "Phone")


def test_devices_persist_as_hashes(tmp_path):
    path = tmp_path / "paired_devices.json"
    svc = PairingService(path)
    code, _ = svc.new_code()
    device, token = svc.redeem(code, "  ")
    assert device.name == "Phone"
    assert token not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = PairingService(path)
    assert again.verify(token).id == device.id
    assert again.verify("not-a-token") is None and again.verify("") is None


def test_computer_codes_need_remote_access(server):
    r = server.post("/api/pairing/codes", json={"kind": "desktop"}, headers=ADMIN)
    assert r.status_code == 409 and "remote access" in r.json()["detail"]


def test_computer_pairs_through_the_link(server, ctx):
    ctx.link.status.running, ctx.link.status.ticket = True, "endpointabc"
    body = server.post("/api/pairing/codes", json={"kind": "desktop"}, headers=ADMIN).json()
    assert body["invite"] == f"endpointabc#{body['code']}" and body["urls"] == []
    # Only the link, which knows the computer's endpoint, can redeem it.
    bare = server.post("/api/pairing/redeem", json={"code": body["code"]})
    assert bare.status_code == 403 and "remote access" in bare.json()["detail"]
    r = server.post(
        "/api/pairing/redeem",
        json={"code": body["code"], "name": "Laptop", "endpoint_id": "ab" * 32},
    )
    assert r.status_code == 200 and r.json()["device"]["kind"] == "desktop"
    laptop = {"Authorization": f"Bearer {r.json()['token']}"}
    # A computer's token opens the whole API, unlike a phone's.
    assert server.get("/api/sessions", headers=laptop).status_code == 200
    assert ctx.pairing.devices()[0].endpoint_id == "ab" * 32


def test_phone_codes_are_not_for_the_link(server):
    code = server.post("/api/pairing/codes", headers=ADMIN).json()["code"]
    r = server.post("/api/pairing/redeem", json={"code": code, "endpoint_id": "ab" * 32})
    assert r.status_code == 403 and "phone" in r.json()["detail"]
    # The failed attempt does not use the code up.
    assert server.post("/api/pairing/redeem", json={"code": code}).status_code == 200
