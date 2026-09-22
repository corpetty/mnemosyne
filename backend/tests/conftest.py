"""Shared fixtures.

The backend resolves DATA_DIR at import time, so the override must be in the
environment before any `src.mnemosyne` module is imported. conftest is loaded
before test modules, which makes this the right place for it.
"""

import os
import tempfile
from pathlib import Path

_DATA_DIR = Path(tempfile.mkdtemp(prefix="mnemosyne-test-"))
os.environ["MNEMOSYNE_DATA_DIR"] = str(_DATA_DIR)
# Never let a developer's real .env leak cloud keys or a vault path into tests.
for _key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OBSIDIAN_VAULT_PATH"):
    os.environ.pop(_key, None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.mnemosyne.api.app import create_app  # noqa: E402
from src.mnemosyne.services import model_service as model_service_module  # noqa: E402
from src.mnemosyne.services.summarization_service import (  # noqa: E402
    summarization_service,
)

from tests.fakes import FakeEngine, FakeProvider  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return _DATA_DIR


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def fake_engine(monkeypatch) -> FakeEngine:
    """Inject a FakeEngine into the ModelService singleton."""
    engine = FakeEngine()
    monkeypatch.setattr(model_service_module.model_service, "_engine", engine)
    return engine


@pytest.fixture
def fake_provider(monkeypatch) -> FakeProvider:
    """Replace all summarization providers with a single FakeProvider named 'fake'."""
    provider = FakeProvider()
    monkeypatch.setattr(summarization_service, "providers", {"fake": provider})
    return provider


@pytest.fixture
def session_with_transcript(client, fake_engine):
    """Create a session and populate its transcript via the fake engine."""
    session = client.post("/api/sessions", json={"name": "Test"}).json()
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "transcribe", "audio_path": "/fake.ogg", "session_id": session["id"]})
        while True:
            msg = ws.receive_json()
            if msg["type"] == "status" and msg["message"] == "Transcription complete":
                break
            if msg["type"] == "error":
                raise AssertionError(msg["message"])
    return client.get(f"/api/sessions/{session['id']}").json()


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio tests on asyncio only (trio is not installed)."""
    return "asyncio"
