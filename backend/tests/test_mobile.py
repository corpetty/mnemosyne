"""Recording from a phone: the page and the address to open on it."""


def test_mobile_page_served(client):
    r = client.get("/m")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/api/audio/import" in r.text and "capture" in r.text


def test_phone_link_local_only(client, monkeypatch):
    monkeypatch.delenv("MNEMOSYNE_BIND_HOST", raising=False)
    body = client.get("/api/server/phone").json()
    assert body["reachable"] is False and "server mode" in body["note"]


def test_phone_link_server_mode(client, ctx, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("MNEMOSYNE_BIND_PORT", "8008")
    monkeypatch.setattr("mnemosyne.api.routes.mobile.lan_addresses", lambda: ["192.168.1.20"])
    body = client.get("/api/server/phone").json()
    assert body["reachable"] is True and body["urls"] == ["http://192.168.1.20:8008/m"]
    assert "API token" in body["note"]
    ctx.settings.api_token = "s3cret"
    body = client.get("/api/server/phone", headers={"Authorization": "Bearer s3cret"}).json()
    assert body["urls"] == ["http://192.168.1.20:8008/m?token=s3cret"]
    # The page itself needs the token in server mode, like the rest of the API.
    assert client.get("/m").status_code == 401
    assert client.get("/m?token=s3cret").status_code == 200
