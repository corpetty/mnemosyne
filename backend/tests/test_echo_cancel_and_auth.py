"""Echo-cancel manager (with a fake pw-cli) and bearer-token server mode."""

import stat

import pytest
from src.mnemosyne.audio import echo_cancel as ec
from src.mnemosyne.audio.echo_cancel import EchoCancelManager, load_command


def test_load_command_names_nodes():
    cmd = load_command()
    assert "libpipewire-module-echo-cancel" in cmd and "monitor.mode = true" in cmd
    assert 'node.name = "mnemosyne_aec_source"' in cmd
    assert cmd.endswith("\n")


@pytest.fixture
def fake_pwcli(tmp_path, monkeypatch):
    """A stand-in pw-cli: records what it was told and stays alive until killed."""
    log = tmp_path / "pwcli.log"
    script = tmp_path / "pw-cli"
    script.write_text(f"#!/bin/bash\ncat >> {log}\nwhile true; do sleep 1; done\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ec, "MODULE_PATHS", (str(script),))  # "module installed"
    monkeypatch.setattr(ec, "find_source_node", lambda node_name=ec.SOURCE_NODE: 146)
    monkeypatch.setattr(ec.shutil, "which", lambda name: str(script))
    return script, log


@pytest.mark.anyio
async def test_manager_start_status_stop(fake_pwcli):
    script, log = fake_pwcli
    m = EchoCancelManager(pw_cli=str(script))
    assert m.supported() == (True, None)
    st = await m.start()
    assert st.active and st.source_node_id == 146
    assert "load-module libpipewire-module-echo-cancel" in log.read_text()
    assert (await m.status()).active
    await m.stop()
    assert not m.active
    assert not (await m.status()).active


def test_manager_unsupported_when_missing(monkeypatch):
    monkeypatch.setattr(ec, "MODULE_PATHS", ("/nonexistent.so",))
    m = EchoCancelManager(pw_cli="pw-cli")
    ok, reason = m.supported()
    assert not ok and "not installed" in reason or "not found" in reason


@pytest.mark.anyio
async def test_manager_stops_when_no_source_appears(fake_pwcli, monkeypatch):
    script, _ = fake_pwcli
    monkeypatch.setattr(ec, "find_source_node", lambda node_name=ec.SOURCE_NODE: None)
    m = EchoCancelManager(pw_cli=str(script))
    st = await m.start()
    assert not st.active and "no source appeared" in st.reason
    assert not m.active


def test_echo_cancel_api(client, ctx, fake_pwcli, monkeypatch):
    script, log = fake_pwcli
    ctx.echo = EchoCancelManager(pw_cli=str(script))
    body = client.get("/api/audio/echo-cancel").json()
    assert body["supported"] and not body["active"] and not body["enabled"]

    body = client.post("/api/audio/echo-cancel", json={"enabled": True}).json()
    assert body["active"] and body["enabled"] and body["source_node_id"] == 146
    assert ctx.settings.echo_cancel is True

    body = client.post("/api/audio/echo-cancel", json={"enabled": False}).json()
    assert not body["active"] and not body["enabled"]


def test_echo_cancel_api_unsupported(client, ctx, monkeypatch):
    monkeypatch.setattr(ec, "MODULE_PATHS", ("/nonexistent.so",))
    ctx.echo = EchoCancelManager(pw_cli="/nonexistent/pw-cli")
    assert client.post("/api/audio/echo-cancel", json={"enabled": True}).status_code == 400


def test_devices_flag_echo_cancelled_source(monkeypatch):
    from src.mnemosyne.audio import capture

    dump = [
        {
            "id": 1,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "node.name": "mnemosyne_aec_source",
                    "node.description": "Mnemosyne: mic (echo cancelled)",
                }
            },
        },
        {
            "id": 2,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {"media.class": "Stream/Input/Audio", "node.name": "mnemosyne_aec_capture"}
            },
        },
        {
            "id": 3,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "node.name": "alsa_input.mic",
                    "node.description": "Mic",
                }
            },
        },
    ]
    import json

    class R:
        returncode = 0
        stdout = json.dumps(dump)
        stderr = ""

    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: R())
    devices = capture.list_devices()
    assert [(d.id, d.is_echo_cancelled) for d in devices] == [(1, True), (3, False)]


# ---- server mode auth -------------------------------------------------------------


def test_no_token_means_open(client):
    assert client.get("/api/sessions").status_code == 200


def test_token_required_when_set(client, ctx):
    ctx.settings.api_token = "s3cret"
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["auth_required"] is True
    r = client.get("/api/sessions")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.get("/api/sessions", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    )
    assert client.get("/api/sessions", params={"token": "s3cret"}).status_code == 200
    assert client.options("/api/sessions").status_code in (200, 405)  # CORS preflight passes auth

    # WebSocket: query token
    with client.websocket_connect("/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] == "hello"
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_token_is_masked_in_settings(client, ctx):
    ctx.settings.api_token = "s3cret"
    body = client.get("/api/settings", headers={"Authorization": "Bearer s3cret"}).json()
    assert body["values"]["api_token"] == "" and body["secrets_set"]["api_token"] is True
