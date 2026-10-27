"""DeepFilterNet behind the echo canceller (audio/noise_suppression.py), with a fake pw-cli."""

import stat
from pathlib import Path

import pytest

from mnemosyne.audio import echo_cancel as ec
from mnemosyne.audio import noise_suppression as ns
from mnemosyne.audio.echo_cancel import EchoCancelManager, load_command
from mnemosyne.audio.noise_suppression import Denoise, filter_chain_command

PLUGIN = Path("/data/models/libdeep_filter_ladspa-0.5.6.so")


def test_load_command_puts_deepfilternet_behind_the_canceller():
    plain = load_command()
    assert plain.count("load-module") == 1 and "filter-chain" not in plain

    cmd = load_command(mic="alsa_input.mic", denoise=Denoise(PLUGIN, 40))
    aec, chain = cmd.strip().split("\n")
    # The canceller: mono, WebRTC's own noise suppression off, its source the internal one.
    assert "libpipewire-module-echo-cancel" in aec and 'target.object = "alsa_input.mic"' in aec
    assert "audio.position = [ MONO ]" in aec and "webrtc.noise_suppression = false" in aec
    assert 'node.name = "mnemosyne_aec_raw"' in aec and "mnemosyne_aec_source" not in aec
    # The noise suppressor reads it and is the source everyone records from.
    assert "libpipewire-module-filter-chain" in chain
    assert f'plugin = "{PLUGIN}" label = deep_filter_mono' in chain
    assert '"Attenuation Limit (dB)" = 40' in chain and "audio.rate = 48000" in chain
    capture = chain[chain.index("capture.props") : chain.index("playback.props")]
    assert 'target.object = "mnemosyne_aec_raw"' in capture and "node.passive = true" in capture
    playback = chain[chain.index("playback.props") :]
    assert 'node.name = "mnemosyne_aec_source"' in playback
    assert "media.class = Audio/Source" in playback


def test_filter_chain_command_clamps_and_refuses_odd_paths():
    assert '"Attenuation Limit (dB)" = 100' in filter_chain_command(Denoise(PLUGIN, 500))
    assert '"Attenuation Limit (dB)" = 1' in filter_chain_command(Denoise(PLUGIN, 0))
    with pytest.raises(ValueError):
        filter_chain_command(Denoise(Path('/x" } evil = { "/p.so')))


def test_the_internal_source_is_never_a_mic():
    with pytest.raises(ValueError):
        ec.valid_mic(ns.RAW_SOURCE_NODE)


@pytest.fixture
def fake_pwcli(tmp_path, monkeypatch):
    """A stand-in pw-cli that records what it was told. The source "appears" unless the last
    thing loaded was a filter chain and `chain_fails` is set."""
    log = tmp_path / "pwcli.log"
    log.touch()
    script = tmp_path / "pw-cli"
    script.write_text(f"#!/bin/bash\ncat >> {log}\nwhile true; do sleep 1; done\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ec, "MODULE_PATHS", (str(script),))
    monkeypatch.setattr(ns, "FILTER_CHAIN_PATHS", (str(script),))
    monkeypatch.setattr(ns.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ec.shutil, "which", lambda name: str(script))
    state = {"chain_fails": False}

    def find(node_name=ec.SOURCE_NODE):
        lines = log.read_text().strip().splitlines()
        if state["chain_fails"] and lines and "filter-chain" in lines[-1]:
            return None
        return 146

    monkeypatch.setattr(ec, "find_source_node", find)
    return script, log, state


@pytest.mark.anyio
async def test_manager_runs_and_restarts_with_noise_suppression(fake_pwcli):
    script, log, _ = fake_pwcli
    m = EchoCancelManager(pw_cli=str(script))
    await m.start("alsa_input.mic")
    st = await m.start("alsa_input.mic", Denoise(PLUGIN, 100))  # turned on: restarted
    assert st.active and st.denoise == Denoise(PLUGIN, 100) and st.denoise_error is None
    first = m._proc
    await m.start("alsa_input.mic", Denoise(PLUGIN, 100))  # the same: left alone
    assert m._proc is first
    assert (await m.status()).denoise == Denoise(PLUGIN, 100)
    await m.start("alsa_input.mic", Denoise(PLUGIN, 18))  # another strength: restarted
    assert m._proc is not first and log.read_text().count("filter-chain") == 2
    await m.stop()


@pytest.mark.anyio
async def test_manager_falls_back_to_plain_echo_cancellation(fake_pwcli):
    script, log, state = fake_pwcli
    state["chain_fails"] = True
    m = EchoCancelManager(pw_cli=str(script))
    st = await m.start(None, Denoise(PLUGIN))
    assert st.active and st.denoise is None and "did not start" in st.denoise_error
    status = await m.status()
    assert status.active and status.denoise is None and status.denoise_error
    # Asked again for the same: not tried over and over.
    first = m._proc
    await m.start(None, Denoise(PLUGIN))
    assert m._proc is first
    await m.stop()


def _fake_download(ctx, monkeypatch):
    calls = []

    def fetch_plugin(models_dir):
        calls.append(models_dir)
        path = Path(models_dir) / ns.PLUGIN.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"plugin")
        return path

    monkeypatch.setattr(ns, "fetch_plugin", fetch_plugin)
    return calls


def test_api_turns_noise_suppression_on_and_off(client, ctx, fake_pwcli, monkeypatch):
    script, log, _ = fake_pwcli
    ctx.echo = EchoCancelManager(pw_cli=str(script))
    calls = _fake_download(ctx, monkeypatch)
    body = client.post("/api/audio/echo-cancel", json={"enabled": True}).json()
    n = body["noise_suppression"]
    assert n["supported"] and not n["enabled"] and not n["active"] and not n["downloaded"]
    assert n["download_mb"] == 53 and n["db"] == 100

    body = client.post(
        "/api/audio/echo-cancel",
        json={"enabled": True, "noise_suppression": True, "noise_suppression_db": 30},
    ).json()
    n = body["noise_suppression"]
    assert n["enabled"] and n["active"] and n["downloaded"] and n["db"] == 30
    assert calls == [ctx.settings.models_dir] and ctx.settings.noise_suppression
    assert '"Attenuation Limit (dB)" = 30' in log.read_text()

    body = client.post(
        "/api/audio/echo-cancel", json={"enabled": True, "noise_suppression": False}
    ).json()
    assert body["active"] and not body["noise_suppression"]["active"]
    assert not ctx.settings.noise_suppression
    bad = {"enabled": True, "noise_suppression_db": 0}
    assert client.post("/api/audio/echo-cancel", json=bad).status_code == 422


def test_api_refuses_where_it_cannot_run(client, ctx, fake_pwcli, monkeypatch):
    script, _, _ = fake_pwcli
    ctx.echo = EchoCancelManager(pw_cli=str(script))
    calls = _fake_download(ctx, monkeypatch)
    monkeypatch.setattr(ns.platform, "machine", lambda: "aarch64")
    r = client.post("/api/audio/echo-cancel", json={"enabled": True, "noise_suppression": True})
    assert r.status_code == 400 and "x86-64" in r.json()["detail"]
    assert calls == [] and not ctx.settings.noise_suppression


def test_api_change_while_recording_waits(client, ctx, fake_pwcli, fake_pipewire, monkeypatch):
    script, log, _ = fake_pwcli
    ctx.echo = EchoCancelManager(pw_cli=str(script))
    _fake_download(ctx, monkeypatch)
    client.post("/api/audio/echo-cancel", json={"enabled": True})
    sid = client.post("/api/audio/start", json={"device_ids": [146]}).json()["session_id"]
    body = client.post(
        "/api/audio/echo-cancel", json={"enabled": True, "noise_suppression": True}
    ).json()
    assert body["noise_suppression"]["pending"] and not body["noise_suppression"]["active"]
    assert "filter-chain" not in log.read_text()
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})

    # The next recording starts it, recording from the new source's node id.
    ids = iter([146, 147])
    monkeypatch.setattr(ec, "find_source_node", lambda node_name=ec.SOURCE_NODE: next(ids, 147))
    sid = client.post("/api/audio/start", json={"device_ids": [146]}).json()["session_id"]
    assert ctx.echo.denoise is not None and "filter-chain" in log.read_text()
    assert [p.device_id for p in ctx.active_recordings[sid].processes] == [147]
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})


@pytest.mark.anyio
async def test_a_crashed_noise_suppressor_leaves_plain_echo_cancellation(fake_pwcli):
    import asyncio

    script, log, _ = fake_pwcli
    m = EchoCancelManager(pw_cli=str(script))
    await m.start(None, Denoise(PLUGIN))
    m._proc.kill()  # DeepFilterNet's panic takes pw-cli down with it
    for _ in range(50):
        await asyncio.sleep(0.1)
        if m.denoise_error:
            break
    st = await m.status()
    assert st.active and st.denoise is None and "too busy" in st.denoise_error
    assert "filter-chain" not in log.read_text().strip().splitlines()[-1]
    first = m._proc
    await m.start(None, Denoise(PLUGIN))  # not retried on its own
    assert m._proc is first
    await m.stop()
    await asyncio.sleep(0.2)
    assert not m.active  # a deliberate stop is not "crashed"


def test_recordings_take_the_mic_from_before_deepfilternet(client, ctx, fake_pwcli, monkeypatch):
    script, _, _ = fake_pwcli
    ctx.echo = EchoCancelManager(pw_cli=str(script))
    _fake_download(ctx, monkeypatch)
    client.post("/api/audio/echo-cancel", json={"enabled": True})
    assert ctx.recording_targets() == {}  # no noise suppressor: the source as it is
    body = client.post(
        "/api/audio/echo-cancel", json={"enabled": True, "noise_suppression": True}
    ).json()
    assert body["noise_suppression"]["record"] is False
    assert ctx.recording_targets() == {"mnemosyne_aec_source": "mnemosyne_aec_raw"}
    body = client.post(
        "/api/audio/echo-cancel", json={"enabled": True, "noise_suppression_record": True}
    ).json()
    assert body["noise_suppression"]["record"] and ctx.recording_targets() == {}


@pytest.mark.anyio
async def test_start_recording_records_the_target_in_place_of_the_device(tmp_path, monkeypatch):
    from mnemosyne.audio import capture
    from mnemosyne.audio.capture import AudioDevice

    devices = [
        AudioDevice(
            id=146, name="mnemosyne_aec_source", description="clean", media_class="Audio/Source"
        ),
        AudioDevice(id=2, name="alsa_input.mic", description="Mic", media_class="Audio/Source"),
    ]
    monkeypatch.setattr(capture, "list_devices", lambda: devices)
    commands = []

    class Proc:
        returncode = None

    async def exec_(*cmd, **_):
        commands.append(cmd)
        return Proc()

    monkeypatch.setattr(capture.asyncio, "create_subprocess_exec", exec_)
    targets = {"mnemosyne_aec_source": "mnemosyne_aec_raw"}
    rec = await capture.start_recording([146, 2], tmp_path, targets=targets)
    assert [p.device_id for p in rec.processes] == [146, 2]
    assert commands[0][commands[0].index("--target") + 1] == "mnemosyne_aec_raw"
    assert commands[1][commands[1].index("--target") + 1] == "alsa_input.mic"
