"""Echo-cancel manager (with a fake pw-cli) and bearer-token server mode."""

import stat

import pytest

from mnemosyne.audio import echo_cancel as ec
from mnemosyne.audio.echo_cancel import EchoCancelManager, load_command


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
    from mnemosyne.audio import capture

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


# ---- the echo canceller's microphone ---------------------------------------------

MIC = "alsa_input.usb-RODE_Microphones_RODE_NT-USB-00.analog-stereo"


def test_load_command_pins_the_chosen_mic():
    cmd = load_command(mic=MIC)
    capture = cmd[cmd.index("capture.props") : cmd.index("source.props")]
    assert f'target.object = "{MIC}"' in capture and "node.dont-reconnect = true" in capture
    default = load_command()
    assert "target.object" not in default and "dont-reconnect" not in default


@pytest.mark.parametrize("bad", ["mnemosyne_aec_source", 'x" } evil = { "', "has space"])
def test_load_command_refuses_our_own_source_and_odd_names(bad):
    with pytest.raises(ValueError):
        load_command(mic=bad)


@pytest.mark.anyio
async def test_a_new_mic_restarts_the_module(fake_pwcli):
    script, log = fake_pwcli
    m = EchoCancelManager(pw_cli=str(script))
    await m.start(MIC)
    first = m._proc
    await m.start(MIC)  # same mic: left alone
    assert m._proc is first and log.read_text().count("load-module") == 1
    st = await m.start("easyeffects_source")
    assert st.active and st.mic == "easyeffects_source" and m.mic == "easyeffects_source"
    assert m._proc is not first and first.returncode is not None  # the old one was stopped
    assert log.read_text().count("load-module") == 2
    await m.stop()


def test_echo_cancel_api_mic_and_deferred_restart(
    client, ctx, fake_pwcli, fake_pipewire, monkeypatch
):
    from mnemosyne.api.routes import audio as audio_routes
    from mnemosyne.audio.capture import AudioDevice

    script, log = fake_pwcli
    ctx.echo = EchoCancelManager(pw_cli=str(script))
    devices = [
        AudioDevice(id=1, name=MIC, description="RODE NT-USB", media_class="Audio/Source"),
        AudioDevice(
            id=5, name="easyeffects_source", description="EasyEffects", media_class="Audio/Source"
        ),
    ]
    monkeypatch.setattr(audio_routes, "list_devices", lambda: devices)

    def echo(mic):
        return client.post("/api/audio/echo-cancel", json={"enabled": True, "mic": mic})

    body = echo(MIC).json()
    assert body["mic"] == MIC and body["mic_description"] == "RODE NT-USB"
    assert ctx.settings.echo_cancel_mic == MIC
    assert echo("mnemosyne_aec_source").status_code == 400

    # Recording from the echo-cancelled source (node 146): a new mic waits.
    sid = client.post("/api/audio/start", json={"device_ids": [146]}).json()["session_id"]
    body = echo("easyeffects_source").json()
    assert body["mic"] == MIC and body["pending_mic"] == "easyeffects_source"
    assert log.read_text().count("load-module") == 1
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})

    # The next recording restarts it; the new source's node id replaces the old one.
    ids = iter([146, 147])
    monkeypatch.setattr(ec, "find_source_node", lambda node_name=ec.SOURCE_NODE: next(ids, 147))
    sid = client.post("/api/audio/start", json={"device_ids": [146]}).json()["session_id"]
    assert ctx.echo.mic == "easyeffects_source" and log.read_text().count("load-module") == 2
    assert [p.device_id for p in ctx.active_recordings[sid].processes] == [147]
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})

    body = echo("").json()
    assert body["mic"] is None and ctx.settings.echo_cancel_mic == ""  # back to the default
