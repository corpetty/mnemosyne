"""Level measurement, tone detection, pw-link parsing, level streaming and self-test API."""

import asyncio

import numpy as np
import pytest

from mnemosyne.audio import levels as L
from mnemosyne.audio.capture import AudioDevice

RATE = 48000


def tone(freq=1000.0, seconds=0.5, amp=0.1, rate=RATE):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def test_level_of():
    assert L.level_of(np.zeros(0, np.int16)).rms_db == L.FLOOR_DB
    assert L.level_of(np.zeros(100, np.int16)).peak_db == L.FLOOR_DB
    lv = L.level_of(tone(amp=0.5))
    assert abs(lv.peak_db - (-6.0)) < 0.2 and abs(lv.rms_db - (-9.0)) < 0.2


def test_tone_level_detects_only_the_right_frequency():
    noise = (np.random.default_rng(0).normal(0, 0.01, RATE) * 32767).astype(np.int16)
    with_tone = noise.copy()
    with_tone[RATE // 4 : RATE // 4 + len(tone())] += tone()
    assert L.tone_level(with_tone, RATE) >= 20
    assert L.tone_level(noise, RATE) < 20
    assert L.tone_level(noise + np.pad(tone(1500), (0, RATE - len(tone(1500)))), RATE) < 20


PW_LINK = """alsa_output.usb.spdif:monitor_FL
  |-> mnemosyne-selftest-abc:input_FL
mnemosyne-selftest-abc:input_FL
  |<- alsa_output.usb.spdif:monitor_FL
mnemosyne-selftest-abc:input_FR
  |<- alsa_output.usb.spdif:monitor_FR
other-rec:input_FL
  |<- alsa_input.usb.rode:capture_FL
"""


def test_linked_sources_parses_pw_link(monkeypatch):
    class R:
        stdout = PW_LINK

    monkeypatch.setattr(L.subprocess, "run", lambda *a, **k: R())
    assert L.linked_sources("mnemosyne-selftest-abc") == [
        "alsa_output.usb.spdif:monitor_FL",
        "alsa_output.usb.spdif:monitor_FR",
    ]
    assert L.linked_sources("other-rec") == ["alsa_input.usb.rode:capture_FL"]
    assert L.linked_sources("nope") == []


SINK = AudioDevice(id=2, name="alsa_output.usb.spdif", description="S", media_class="Audio/Sink")


@pytest.fixture
def fake_selftest(monkeypatch, tmp_path):
    """Replace pw-record/pw-play with Python that writes a WAV, optionally with the tone."""
    state = {"links": ["alsa_output.usb.spdif:monitor_FL"], "tone": True}

    async def fake_exec(*cmd, **kw):
        class P:
            returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        if cmd[0] == "pw-record":
            import wave

            out = cmd[-1]
            pcm = np.zeros(RATE, np.int16)
            if state["tone"]:
                pcm[RATE // 3 : RATE // 3 + len(tone())] = tone()
            with wave.open(out, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(pcm.tobytes())
        return P()

    monkeypatch.setattr(L.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(L, "linked_sources", lambda node: list(state["links"]))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(L.asyncio, "sleep", lambda s: real_sleep(0))
    return state


@pytest.mark.anyio
async def test_self_test_outcomes(fake_selftest):
    r = await L.self_test(SINK)
    assert r.passed and r.captures_monitor and r.tone_detected and "works" in r.message

    fake_selftest["links"] = ["alsa_input.usb.rode:capture_FL"]
    r = await L.self_test(SINK)
    assert not r.passed and not r.captures_monitor
    assert "NOT being captured" in r.message and "alsa_input.usb.rode" in r.message

    fake_selftest["links"] = ["alsa_output.usb.spdif:monitor_FL"]
    fake_selftest["tone"] = False
    r = await L.self_test(SINK)
    assert not r.passed and r.captures_monitor and "not heard" in r.message

    fake_selftest["links"] = []
    assert "did not connect" in (await L.self_test(SINK)).message


def test_self_test_api_validation(client, ctx, fake_pipewire, fake_selftest, monkeypatch):
    from mnemosyne.api.routes import audio as audio_routes

    monkeypatch.setattr(audio_routes, "list_devices", lambda: fake_pipewire)
    assert client.post("/api/audio/self-test", json={"device_id": 1}).status_code == 400  # a mic
    assert client.post("/api/audio/self-test", json={"device_id": 99}).status_code == 404
    body = client.post("/api/audio/self-test", json={"device_id": 2}).json()
    assert set(body) >= {"passed", "linked_from", "message", "level", "tone_snr_db"}
    ctx.active_recordings["x"] = object()
    assert client.post("/api/audio/self-test", json={"device_id": 2}).status_code == 409
    ctx.active_recordings.clear()


def test_levels_streamed_while_recording(client, ctx, fake_pipewire, tmp_path):
    import wave

    ctx.settings.live_transcription = False
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
        rec = ctx.active_recordings[sid]
        path = rec.processes[0].output_path
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(tone(amp=0.5).tobytes())
        for _ in range(50):
            msg = ws.receive_json()
            if msg["type"] == "levels":
                break
        assert msg["session_id"] == sid
        assert abs(msg["levels"]["1"]["peak_db"] - (-6.0)) < 0.3
        client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
    assert sid not in ctx.level_tasks
