"""Shared fixtures. Every test gets a fresh app with its own temp data dir and
config file, a FakeEngine in place of WhisperX, and no real LLM providers."""

import os

import pytest
from fastapi.testclient import TestClient
from src.mnemosyne.api.app import create_app
from src.mnemosyne.config import Settings

from tests.fakes import FakeEngine, FakeProvider

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
