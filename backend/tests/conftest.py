"""Shared fixtures. Every test gets a fresh app with its own temp data dir and
config file, a FakeEngine in place of WhisperX, and no real LLM providers."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.mnemosyne.api.app import create_app
from src.mnemosyne.api.routes import audio as audio_routes
from src.mnemosyne.audio.capture import AudioDevice, RecordingProcess, RecordingSession
from src.mnemosyne.config import Settings

from tests.fakes import FakeEngine, FakeProvider, FakeTranscriber

# config.py loads backend/.env on import. Scrub every settings field from the
# environment so a developer's keys, vault, or model choices never leak into tests.
for _name in Settings.model_fields:
    os.environ.pop(_name.upper(), None)
for _key in ("MNEMOSYNE_DATA_DIR", "MNEMOSYNE_CONFIG_FILE"):
    os.environ.pop(_key, None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def ctx(app):
    return app.state.ctx


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fake_live_transcriber(monkeypatch) -> FakeTranscriber:
    """Never build a real (model-downloading) live transcriber in tests."""
    from src.mnemosyne.services.model_service import ModelService

    fake = FakeTranscriber()
    monkeypatch.setattr(ModelService, "live_transcriber", property(lambda self: fake))
    return fake


@pytest.fixture
def fake_engine(ctx) -> FakeEngine:
    engine = FakeEngine()
    ctx.models._engine = engine
    return engine


@pytest.fixture
def fake_provider(ctx) -> FakeProvider:
    provider = FakeProvider()
    ctx.summarizer.providers = {"fake": provider}
    return provider


def drain_until_job(ws, job_id: str) -> list[dict]:
    """Read WS events until the given job reaches a terminal state."""
    events = []
    while True:
        msg = ws.receive_json()
        events.append(msg)
        if msg["type"] == "job" and msg["job"]["id"] == job_id:
            if msg["job"]["status"] in ("completed", "failed", "cancelled"):
                return events


def run_summarize(client, session_id: str, body: dict | None = None) -> dict:
    """POST /summarize and wait for the job; return the final job record."""
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        resp = client.post(f"/api/sessions/{session_id}/summarize", json=body or {})
        assert resp.status_code == 200, resp.text
        job = resp.json()
        assert job["kind"] == "summarize"
        drain_until_job(ws, job["id"])
    return client.get(f"/api/jobs/{job['id']}").json()


@pytest.fixture
def transcribed_session(client, ctx, fake_engine) -> dict:
    """A session whose transcript was produced by the FakeEngine via the job runner."""
    sid = client.post("/api/sessions", json={"name": "Transcribed"}).json()["id"]
    ctx.sessions.set_audio(sid, "/fake/mixed.ogg", [])
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post(f"/api/sessions/{sid}/transcribe").json()
        drain_until_job(ws, job["id"])
    return client.get(f"/api/sessions/{sid}").json()


class _FakeProc:
    returncode = 0


@pytest.fixture
def fake_pipewire(monkeypatch, tmp_path):
    devices = [
        AudioDevice(id=1, name="mic", description="Built-in Mic", media_class="Audio/Source"),
        AudioDevice(id=2, name="spk", description="Speakers", media_class="Audio/Sink"),
    ]

    async def start_recording(device_ids, output_dir, **_):
        output_dir.mkdir(parents=True, exist_ok=True)
        session = RecordingSession(session_id="rec00001", output_dir=output_dir)
        for d in device_ids:
            session.processes.append(
                RecordingProcess(
                    device_id=d, process=_FakeProc(), output_path=output_dir / f"dev{d}.wav"
                )
            )
        session.is_recording = True
        return session

    async def stop_recording(session):
        session.is_recording = False
        files = []
        for p in session.processes:
            path = p.output_path.with_suffix(".ogg")
            path.write_bytes(b"ogg")
            files.append(path)
        return files

    def mix_audio_files(inputs, output):
        output = Path(output).with_suffix(".ogg")
        output.write_bytes(b"mixed")
        return output

    monkeypatch.setattr(audio_routes, "list_devices", lambda: devices)
    monkeypatch.setattr(audio_routes, "start_recording", start_recording)
    monkeypatch.setattr(audio_routes, "stop_recording", stop_recording)
    monkeypatch.setattr(audio_routes, "mix_audio_files", mix_audio_files)
    return devices
