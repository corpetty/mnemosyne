"""A first run that is ready when you are: model downloads say how far along they are
(services/model_downloads.py), and setup fetches the models up front (POST /api/system/prepare)."""

import asyncio

from mnemosyne.services import model_downloads
from tests.conftest import drain_until_job
from tests.fakes import FakeEngine


class DownloadingEngine(FakeEngine):
    """Loads by writing a 'model' into the Hugging Face cache, a megabyte at a time."""

    def __init__(self, mb: int = 8):
        super().__init__()
        self.mb = mb

    async def load(self) -> None:
        folder = model_downloads.hf_cache() / "models--x--y" / "blobs"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "abc.incomplete", "wb") as f:
            for _ in range(self.mb):
                f.write(b"\0" * model_downloads.MB)
                f.flush()
                await asyncio.sleep(0.15)  # slower than the watch's half-second look
        self.loaded = True


def test_a_download_is_reported_against_the_expected_size(tmp_path):
    seen = []

    async def work():
        with open(tmp_path / "model.bin", "wb") as f:
            for _ in range(5):
                f.write(b"\0" * model_downloads.MB)
                f.flush()
                await asyncio.sleep(0.03)
        return "loaded"

    async def run():
        return await model_downloads.watched(
            work(), [tmp_path], 100, lambda done, total: seen.append((done, total)), interval=0.02
        )

    assert asyncio.run(run()) == "loaded"
    assert seen and seen[-1][1] == 100 and 2 <= seen[-1][0] <= 5


def test_a_model_already_here_is_not_a_download(tmp_path):
    (tmp_path / "model.bin").write_bytes(b"\0" * 5 * model_downloads.MB)
    seen = []

    async def run():
        await model_downloads.watched(
            asyncio.sleep(0.1), [tmp_path], 100, lambda *a: seen.append(a), interval=0.02
        )

    asyncio.run(run())
    assert seen == []


def test_the_expected_size_follows_the_engines(settings, monkeypatch):
    from mnemosyne.transcription import registry

    monkeypatch.setattr(registry, "installed", lambda *modules: True)  # CI has no ML packages
    settings.transcriber, settings.diarizer = "parakeet", "onnx"
    assert model_downloads.speech_mb(settings) == 690 + 45
    settings.diarizer = "none"
    assert model_downloads.speech_mb(settings) == 690


def test_a_transcription_says_it_is_downloading(client, ctx, monkeypatch):
    ctx.models._engine = DownloadingEngine()
    monkeypatch.setattr(model_downloads, "speech_mb", lambda settings: 735)
    messages = []
    on = lambda done, total: messages.append((done, total))  # noqa: E731
    client.portal.call(lambda: ctx.models.ensure_loaded(on_download=on))
    assert messages and messages[-1][1] == 735 and ctx.models._engine.loaded


def test_setup_prepares_the_models(client, ctx, monkeypatch):
    ctx.models._engine = DownloadingEngine()
    monkeypatch.setattr(model_downloads, "speech_mb", lambda settings: 735)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/system/prepare").json()
        assert client.post("/api/system/prepare").status_code == 409  # one at a time
        events = drain_until_job(ws, job["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed", done
    messages = [e["job"]["message"] for e in events if e.get("type") == "job"]
    assert any("Downloading the speech models" in m and "of about 735 MB" in m for m in messages)
    assert ctx.models._engine.loaded
    system = client.get("/api/system").json()
    assert system["gpu_support"] == "auto" and "setup_complete" in system
