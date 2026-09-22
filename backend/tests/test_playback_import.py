"""Audio streaming with range support, and importing existing files."""

import shutil
import wave

import numpy as np
import pytest

from tests.conftest import drain_until_job

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_audio_endpoint_404s(client, ctx):
    assert client.get("/api/audio/file/nope").status_code == 404
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.get(f"/api/audio/file/{sid}").status_code == 404  # no audio yet
    ctx.sessions.set_audio(sid, "/does/not/exist.ogg", [])
    assert client.get(f"/api/audio/file/{sid}").status_code == 404
    assert client.get(f"/api/audio/file/{sid}", params={"recording": "x"}).status_code == 404


def test_audio_endpoint_streams_with_ranges(client, ctx, tmp_path):
    audio = tmp_path / "mixed.ogg"
    audio.write_bytes(bytes(range(256)) * 4)
    sid = client.post("/api/sessions", json={}).json()["id"]
    ctx.sessions.set_audio(sid, str(audio), [])
    full = client.get(f"/api/audio/file/{sid}")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("audio/ogg")
    assert len(full.content) == 1024
    part = client.get(f"/api/audio/file/{sid}", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10


def _wav(path, seconds=1.0):
    rate = 16000
    t = np.arange(int(rate * seconds)) / rate
    pcm = (np.sin(2 * np.pi * 440 * t) * 0.3 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


@needs_ffmpeg
def test_import_creates_session_and_transcribes(client, ctx, fake_engine, tmp_path):
    src = tmp_path / "Board call.wav"
    _wav(src)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        with src.open("rb") as f:
            resp = client.post(
                "/api/audio/import", files={"file": ("Board call.wav", f, "audio/wav")}
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["job_id"]
        drain_until_job(ws, body["job_id"])

    session = body["session"]
    assert session["name"] == "Board call"
    assert session["audio_file"].endswith("import_mixed.ogg")
    assert session["recordings"][0]["source"] == "import"
    assert session["recordings"][0]["device_name"] == "Board call.wav"
    # the pipeline transcribed the mixed file as a single source
    assert fake_engine.sources[0][0].kind == "mixed"
    final = client.get(f"/api/sessions/{session['id']}").json()
    assert final["status"] == "completed" and len(final["transcript"]) == 3
    # and it can be played back
    assert client.get(f"/api/audio/file/{session['id']}").status_code == 200
    rec = session["recordings"][0]["id"]
    assert (
        client.get(f"/api/audio/file/{session['id']}", params={"recording": rec}).status_code == 200
    )


@needs_ffmpeg
def test_import_options_and_errors(client, ctx, tmp_path):
    src = tmp_path / "x.wav"
    _wav(src, 0.2)
    with src.open("rb") as f:
        resp = client.post(
            "/api/audio/import",
            files={"file": ("x.wav", f, "audio/wav")},
            data={"name": "Renamed", "transcribe": "false"},
        )
    assert resp.status_code == 200
    assert resp.json()["session"]["name"] == "Renamed" and resp.json()["job_id"] is None

    resp = client.post("/api/audio/import", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert resp.status_code == 400
    resp = client.post("/api/audio/import", files={"file": ("empty.wav", b"", "audio/wav")})
    assert resp.status_code == 400
    resp = client.post("/api/audio/import", files={"file": ("bad.mp3", b"not audio", "audio/mpeg")})
    assert resp.status_code == 400
    # failed imports do not leave orphaned sessions except the decode failure (marked error)
    names = [s["name"] for s in client.get("/api/sessions").json()]
    assert "empty" not in names
