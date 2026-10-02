"""A team from one desktop install (services/team_host.py, routes/team.py): the owner, the
network listener with its own certificate, and who may turn it on."""

import shutil
import socket
import ssl
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from mnemosyne.api.app import create_app
from mnemosyne.services import team_host

LOCAL = "http://127.0.0.1:8008"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def desktop(settings, keystore):
    """The desktop app's backend: requests from 127.0.0.1, as the app makes them."""
    settings.team_port = _free_port()
    app = create_app(settings, keystore=keystore)
    with TestClient(app, base_url=LOCAL, client=("127.0.0.1", 50000)) as client:
        yield client, app.state.ctx


def test_sharing_makes_you_the_admin_and_keeps_your_meetings(desktop):
    client, ctx = desktop
    before = client.post("/api/sessions", json={"name": "Before sharing"}).json()["id"]
    assert client.get("/api/team").json()["can_change"] is True
    r = client.put("/api/team", json={"enabled": True, "name": "Corey"})
    assert r.status_code == 200, r.text
    status = r.json()
    assert status["enabled"] and status["running"] and status["owner"] == "Corey"
    assert all(a.startswith("https://") for a in status["addresses"])
    # The desktop app needs no token and is the owner; the meeting made before is theirs.
    me = client.get("/api/users/me").json()
    assert me["name"] == "Corey" and me["role"] == "admin" and me["team_mode"] is True
    assert client.get(f"/api/sessions/{before}").json()["owner_id"] == me["id"]

    # The network listener: HTTPS with the certificate made for this machine, sign-in needed.
    url = f"https://127.0.0.1:{ctx.settings.team_port}"
    with httpx.Client(verify=False, timeout=5) as net:
        assert net.get(f"{url}/health").json()["team_mode"] is True
        assert net.get(f"{url}/api/sessions").status_code == 401
        code, _ = ctx.users.invite(ctx.users.add("Sam", "", "member").id)
        token = net.post(f"{url}/api/users/redeem", json={"code": code, "device": "x"}).json()[
            "token"
        ]
        sam = {"Authorization": f"Bearer {token}"}
        assert net.get(f"{url}/api/users/me", headers=sam).json()["name"] == "Sam"
        assert net.get(f"{url}/api/sessions/{before}", headers=sam).status_code == 404
        # Over the network, sharing cannot be switched off, not even by an admin.
        assert net.get(f"{url}/api/team", headers=sam).json()["can_change"] is False
    cert = (ctx.settings.data_dir / "tls" / "team.crt").read_bytes()
    assert b"BEGIN CERTIFICATE" in cert

    r = client.put("/api/team", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["running"] is False
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{url}/health", verify=False, timeout=2)
    assert client.get("/api/users/me").json()["team_mode"] is False


def test_a_proxy_on_this_machine_is_not_the_owner(desktop):
    """`tailscale serve` or Caddy pass requests on from 127.0.0.1 too: they still sign in."""
    client, ctx = desktop
    client.put("/api/team", json={"enabled": True, "name": "Corey"})
    assert client.get("/api/users/me").json()["name"] == "Corey"
    proxied = [
        {"Host": "desk.tail1234.ts.net"},
        {"X-Forwarded-For": "100.64.0.7"},
        {"Forwarded": "for=100.64.0.7"},
    ]
    for headers in proxied:
        assert client.get("/api/users/me", headers=headers).status_code == 401, headers
    assert client.get("/api/users/me", headers={"Host": "[::1]:8008"}).status_code == 200
    client.put("/api/team", json={"enabled": False})


def test_only_the_desktop_app_turns_sharing_on(settings, keystore):
    settings.team_port = _free_port()
    with TestClient(create_app(settings, keystore=keystore)) as client:  # not from 127.0.0.1
        assert client.put("/api/team", json={"enabled": True}).status_code == 403


def test_the_certificate_names_this_machine_and_is_made_again_when_that_changes(tmp_path):
    cert, key = team_host.ensure_certificate(tmp_path, ["box", "192.168.1.20"])
    first = cert.read_bytes()
    assert key.stat().st_mode & 0o777 == 0o600
    team_host.ensure_certificate(tmp_path, ["box", "192.168.1.20"])
    assert cert.read_bytes() == first  # unchanged names: kept
    team_host.ensure_certificate(tmp_path, ["box", "10.0.0.5"])
    assert cert.read_bytes() != first
    ctx = ssl.create_default_context()
    ctx.load_cert_chain(cert, key)  # a usable pair


def test_a_port_in_use_is_reported(desktop):
    client, ctx = desktop
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", ctx.settings.team_port))
        busy.listen()
        r = client.put("/api/team", json={"enabled": True})
    assert r.status_code == 409 and str(ctx.settings.team_port) in r.json()["detail"]


def test_options_are_saved_and_health_says_whether_it_outlives_the_app(desktop):
    client, ctx = desktop
    assert client.get("/health").json()["outlives_app"] is False
    r = client.put("/api/team", json={"keep_sharing_after_quit": True})
    assert r.json()["keep_sharing_after_quit"] is True and r.json()["enabled"] is False
    assert client.get("/health").json()["outlives_app"] is False  # not shared
    client.put("/api/team", json={"enabled": True, "name": "Corey"})
    assert client.get("/health").json()["outlives_app"] is True
    client.put("/api/team", json={"keep_awake_while_sharing": True})
    assert ctx.settings.keep_awake_while_sharing and ctx.settings.share_on_network
    client.put("/api/team", json={"enabled": False})
    assert client.get("/health").json()["outlives_app"] is False


def _served_cert(port: int, name: str | None = None):
    """The certificate the listener answers with, asked for `name` by SNI (None: none sent)."""
    from cryptography import x509

    tls = ssl.create_default_context()
    tls.check_hostname, tls.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        with tls.wrap_socket(sock, server_hostname=name) as s:
            der = s.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [str(v) for v in san.get_values_for_type(x509.DNSName)] + [
        str(v) for v in san.get_values_for_type(x509.IPAddress)
    ]


def test_a_laptop_on_another_network_gets_a_certificate_for_it(desktop, monkeypatch):
    client, ctx = desktop
    monkeypatch.setattr(team_host, "lan_addresses", lambda: ["192.168.1.20"])
    client.put("/api/team", json={"enabled": True, "name": "Corey"})
    port = ctx.settings.team_port
    assert "192.168.1.20" in _served_cert(port)
    assert client.portal.call(ctx.team_host.check_addresses) is False  # nothing changed
    monkeypatch.setattr(team_host, "lan_addresses", lambda: ["10.9.8.7"])
    assert client.portal.call(ctx.team_host.check_addresses) is True
    names = _served_cert(port)  # the running listener, no restart
    assert "10.9.8.7" in names and "192.168.1.20" not in names
    client.put("/api/team", json={"enabled": False})


@pytest.fixture
def tailnet(monkeypatch, tmp_path):
    """A Tailscale that gives certificates for bean.tail1234.ts.net (or refuses)."""
    name = "bean.tail1234.ts.net"
    issued, _ = team_host.ensure_certificate(tmp_path / "issuer", [name])
    calls = []
    state = {"refuse": False}

    def run(args, timeout):
        calls.append(args)
        assert args[:2] == ["tailscale", "cert"] and args[-1] == name
        if state["refuse"]:
            return subprocess.CompletedProcess(args, 1, "", "Access denied: cert access denied")
        cert, key = args[args.index("--cert-file") + 1], args[args.index("--key-file") + 1]
        shutil.copy(issued, cert)
        shutil.copy(tmp_path / "issuer" / "team.key", key)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(team_host, "tailscale_name", lambda: name)
    monkeypatch.setattr(team_host, "_run", run)
    return SimpleNamespace(name=name, calls=calls, state=state)


def test_the_tailscale_name_gets_its_real_certificate(desktop, tailnet, monkeypatch):
    client, ctx = desktop
    client.put("/api/team", json={"enabled": True, "name": "Corey"})
    status = client.get("/api/team").json()
    assert status["tailscale_name"] == tailnet.name and status["tailscale_cert"] is False
    r = client.put("/api/team", json={"tailscale_cert": True})
    assert r.status_code == 200, r.text
    port = ctx.settings.team_port
    assert r.json()["addresses"][0] == f"https://{tailnet.name}:{port}"
    assert ctx.settings.team_tailscale_cert is True
    assert _served_cert(port, tailnet.name) == [tailnet.name]  # by SNI
    assert tailnet.name not in _served_cert(port)  # by address: the self-signed one
    # Fresh: not fetched again. With under 30 days left: renewed.
    client.portal.call(ctx.team_host.use_tailscale)
    assert len(tailnet.calls) == 1
    monkeypatch.setattr(team_host, "days_left", lambda cert: 10)
    client.portal.call(ctx.team_host.use_tailscale)
    assert len(tailnet.calls) == 2
    client.put("/api/team", json={"tailscale_cert": False})
    assert tailnet.name not in _served_cert(port, tailnet.name)
    client.put("/api/team", json={"enabled": False})


def test_tailscale_refusing_says_what_to_do(desktop, tailnet):
    client, ctx = desktop
    tailnet.state["refuse"] = True
    client.put("/api/team", json={"enabled": True, "name": "Corey"})
    r = client.put("/api/team", json={"tailscale_cert": True})
    assert r.status_code == 409 and "tailscale set --operator=" in r.json()["detail"]
    assert ctx.settings.team_tailscale_cert is False
    client.put("/api/team", json={"enabled": False})
