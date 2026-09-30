"""Recording from a browser (a firm's server): its audio arrives over a WebSocket per source
and is saved like pw-record's."""

import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from mnemosyne import access
from mnemosyne.api.routes import audio as audio_routes
from mnemosyne.audio.capture import (
    BROWSER_SOURCES,
    BrowserProcess,
    label_of,
    source_of,
    start_browser_recording,
)
from mnemosyne.transcription.live import WavTail
from tests.conftest import stop_and_finish

RATE = 16000


def _pcm(seconds: float, value: int = 1000) -> bytes:
    return np.full(int(RATE * seconds), value, dtype="<i2").tobytes()


def _start(client, sources=("mic", "system"), **extra):
    body = {"sources": list(sources), "sample_rate": RATE, **extra}
    r = client.post("/api/audio/start-browser", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_browser_sources_are_files_like_any_recorders(tmp_path):
    rec = start_browser_recording(["mic", "system", "mic"], tmp_path, 48000, {"mic": "USB mic"})
    assert [p.source for p in rec.processes] == ["mic", "system"]
    assert [p.device_id for p in rec.processes] == [BROWSER_SOURCES["mic"], -2]
    assert all(isinstance(p.process, BrowserProcess) for p in rec.processes)
    assert [label_of(p, {}) for p in rec.processes] == ["USB mic", "Call audio (browser)"]
    assert [source_of(p, {}) for p in rec.processes] == ["mic", "system"]
    tail = WavTail(rec.processes[0].output_path)
    assert tail.read_new().size == 0 and tail.sample_rate == 48000


def test_audio_sent_by_the_browser_is_saved_as_the_meetings_parts(client, ctx, fake_pipewire):
    started = _start(client, labels={"mic": "MacBook microphone"})
    sid, rid = started["session_id"], started["recording_id"]
    recording = ctx.active_recordings[sid]
    with client.websocket_connect(f"/api/record/{rid}/mic") as mic:
        with client.websocket_connect(f"/api/record/{rid}/system") as call:
            mic.send_bytes(_pcm(1.0))
            mic.send_bytes(_pcm(0.5) + b"\x01")  # an odd byte is dropped, never half a sample
            call.send_bytes(_pcm(2.0, 50))
    paths = {p.source: p.output_path for p in recording.processes}
    assert WavTail(paths["mic"]).read_new().size == int(RATE * 1.5)
    assert WavTail(paths["system"]).read_new().size == RATE * 2
    stop_and_finish(client, sid)
    session = client.get(f"/api/sessions/{sid}").json()
    assert {(r["source"], r["device_name"]) for r in session["recordings"]} == {
        ("mic", "MacBook microphone"),
        ("system", "Call audio (browser)"),
    }
    events = [e["kind"] for e in ctx.repo.events(sid)]
    assert "recording_started" in events and "part_saved" in events


def test_nothing_is_written_after_stop(client, ctx, fake_pipewire):
    started = _start(client, sources=("mic",))
    sid, rid = started["session_id"], started["recording_id"]
    path = ctx.active_recordings[sid].processes[0].output_path
    with client.websocket_connect(f"/api/record/{rid}/mic") as ws:
        ws.send_bytes(_pcm(0.5))
    size = path.stat().st_size
    stop_and_finish(client, sid)
    # The recording is gone: a late connection finds nothing to write into.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/record/{rid}/mic") as ws:
            ws.send_bytes(_pcm(0.5))
            ws.receive_bytes()
    assert not path.exists() or path.stat().st_size == size


def test_unknown_recordings_and_sources_are_refused(client, fake_pipewire):
    started = _start(client, sources=("mic",))
    for url in ("/api/record/nope/mic", f"/api/record/{started['recording_id']}/system"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url) as ws:
                ws.receive_bytes()


def test_only_the_meetings_owner_can_send_audio(settings, keystore, fake_pipewire):
    from fastapi.testclient import TestClient

    from mnemosyne.api.app import create_app

    settings.firm_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    tokens = {}
    for name in ("Ann", "Bob"):
        user = ctx.users.add(name, "", "advisor" if name != "Ann" else "admin")
        code, _ = ctx.users.invite(user.id)
        tokens[name] = ctx.users.redeem(code, "x")[1]
    with TestClient(app) as client:
        r = client.post(
            "/api/audio/start-browser",
            json={"sources": ["mic"], "sample_rate": RATE},
            headers={"Authorization": f"Bearer {tokens['Ann']}"},
        )
        rid = r.json()["recording_id"]
        with pytest.raises(WebSocketDisconnect):  # no token
            with client.websocket_connect(f"/api/record/{rid}/mic") as ws:
                ws.receive_bytes()
        with pytest.raises(WebSocketDisconnect):  # someone else's meeting
            with client.websocket_connect(f"/api/record/{rid}/mic?token={tokens['Bob']}") as ws:
                ws.receive_bytes()
        with client.websocket_connect(f"/api/record/{rid}/mic?token={tokens['Ann']}") as ws:
            ws.send_bytes(_pcm(0.25))
    assert access.current.get() is None


def test_a_reconnect_takes_over_the_source(client, ctx, fake_pipewire):
    started = _start(client, sources=("mic",))
    sid, rid = started["session_id"], started["recording_id"]
    path = ctx.active_recordings[sid].processes[0].output_path
    with client.websocket_connect(f"/api/record/{rid}/mic") as first:
        first.send_bytes(_pcm(0.5))
        with client.websocket_connect(f"/api/record/{rid}/mic") as second:
            second.send_bytes(_pcm(0.5, 7))
    assert WavTail(path).read_new().size == RATE  # both halves, one after the other


def test_restart_is_the_browsers_job(client, fake_pipewire):
    started = _start(client, sources=("mic",))
    assert client.post(f"/api/audio/restart/{started['session_id']}").status_code == 400


def test_bad_requests(client, fake_pipewire):
    assert client.post("/api/audio/start-browser", json={"sources": []}).status_code == 400
    r = client.post("/api/audio/start-browser", json={"sources": ["mic"], "sample_rate": 5})
    assert r.status_code == 400
    r = client.post("/api/audio/start-browser", json={"sources": ["speakers"]})
    assert r.status_code == 422


def test_a_browser_that_went_away_is_stopped(client, ctx, fake_pipewire, monkeypatch):
    import time

    monkeypatch.setattr(audio_routes, "BROWSER_GONE_SECONDS", 0.3)
    started = _start(client, sources=("mic",))
    sid = started["session_id"]
    deadline = time.monotonic() + 5
    while sid in ctx.active_recordings and time.monotonic() < deadline:
        time.sleep(0.1)
    assert sid not in ctx.active_recordings
    assert "browser_gone" in [e["detail"].get("reason") for e in ctx.repo.events(sid)]


def test_stopping_fixes_the_headers_and_drops_silent_sources(tmp_path):
    """The real encoder path: sizes written into the header, empty sources left out."""
    import asyncio
    import shutil
    import subprocess

    from mnemosyne.audio.capture import stop_recording

    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    rec = start_browser_recording(["mic", "system"], tmp_path, RATE, {})
    with rec.processes[0].output_path.open("ab") as f:
        f.write(_pcm(1.0))
    files = asyncio.run(stop_recording(rec))
    assert files[1] is None  # the call audio was never shared: nothing to save
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(files[0])],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert abs(float(probe.stdout) - 1.0) < 0.1
