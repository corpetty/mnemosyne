"""Summaries with nothing else installed: the built-in model (services/local_llm.py,
summarization/local.py, routes/local_model.py) and pulling a model into Ollama."""

import asyncio
import io
import json
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest

from mnemosyne.services import downloads, local_llm
from mnemosyne.summarization.local import LocalProvider
from tests.conftest import drain_until_job


def _fake_release(path: Path) -> None:
    """A tarball shaped like llama.cpp's: one folder with a binary, libraries and a symlink."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode in [
            ("llama-b0/llama-server", b"#!/bin/sh\n", 0o755),
            ("llama-b0/libllama.so.0.1", b"lib", 0o644),
        ]:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("llama-b0/libllama.so.0")
        link.type, link.linkname = tarfile.SYMTYPE, "libllama.so.0.1"
        tar.addfile(link)


@pytest.fixture
def no_network(monkeypatch, tmp_path):
    """Downloads write small stand-ins instead of fetching."""
    fetched = []

    def fetch(folder, item, progress=None):
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / item.name
        if dest.is_file():  # as the real one: what is here is not fetched again
            return dest
        if item.name.endswith(".tar.gz"):
            _fake_release(dest)
        else:
            dest.write_bytes(b"gguf")
        if progress:
            progress(item.size, item.size)
        fetched.append(item.name)
        return dest

    monkeypatch.setattr(local_llm, "fetch", fetch)
    monkeypatch.setattr(local_llm, "has_vulkan", lambda: False)
    return fetched


def test_the_model_offered_fits_the_memory(monkeypatch):
    monkeypatch.setattr(local_llm, "ram_gb", lambda: 16)
    assert local_llm.recommended() == "qwen3-4b"
    monkeypatch.setattr(local_llm, "ram_gb", lambda: 31)  # a 32 GB computer
    assert local_llm.recommended() == "qwen3-30b-a3b"


def test_install_unpacks_the_server_flat_and_fetches_the_model(tmp_path, no_network):
    llm = local_llm.LocalLLM(tmp_path / "llm")
    seen = []
    llm.install("qwen3-4b", lambda done, total: seen.append((done, total)))
    assert llm.server_binary().is_file() and (llm.server_binary().stat().st_mode & 0o111)
    assert (llm.server_dir() / "libllama.so.0").is_symlink()
    assert llm.ready("qwen3-4b") and llm.downloaded() == ["qwen3-4b"]
    assert no_network[0].endswith("-cpu.tar.gz")  # no Vulkan here: the CPU build
    assert seen[-1][0] == seen[-1][1]  # progress reaches the total of both downloads
    llm.install("qwen3-4b")  # already here: nothing fetched again
    assert len(no_network) == 2


def test_the_provider_lists_what_is_downloaded(tmp_path, no_network):
    llm = local_llm.LocalLLM(tmp_path / "llm")
    provider = LocalProvider(llm)
    assert asyncio.run(provider.list_models()) == []
    llm.install("qwen3-4b")
    assert asyncio.run(provider.list_models()) == ["qwen3-4b"]
    with pytest.raises(RuntimeError, match="not downloaded"):
        asyncio.run(llm.url("qwen3-30b-a3b"))


def test_a_missing_server_is_fetched_for_a_model_already_here(tmp_path, no_network, monkeypatch):
    llm = local_llm.LocalLLM(tmp_path / "llm")
    llm.install("qwen3-4b")
    shutil.rmtree(llm.server_dir())
    started = []
    monkeypatch.setattr(llm, "_start", lambda m: started.append(m))

    async def healthy():
        return None

    monkeypatch.setattr(llm, "_wait_healthy", healthy)
    asyncio.run(llm.url("qwen3-4b"))
    assert llm.server_binary().is_file() and started == ["qwen3-4b"]


def test_one_manager_per_data_folder_and_a_provider_named_local(settings):
    assert local_llm.manager(settings.models_dir) is local_llm.manager(settings.models_dir)
    from mnemosyne.services.summarization_service import SummarizationService

    assert "local" in SummarizationService(settings).providers


def test_status_and_download_through_the_api(client, ctx, monkeypatch, no_network):
    monkeypatch.setattr(local_llm, "ram_gb", lambda: 16)
    ctx.http_transport = httpx.MockTransport(lambda r: httpx.Response(503))  # no Ollama
    status = client.get("/api/local-model").json()
    assert status["ollama"] == {
        "reachable": False,
        "models": [],
        "suggested": "qwen3:4b-instruct-2507-q4_K_M",
    }
    small = next(m for m in status["models"] if m["id"] == "qwen3-4b")
    assert small["recommended"] and not small["downloaded"] and small["size"] > 2e9
    assert client.post("/api/local-model/download", json={"model": "x"}).status_code == 400
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/local-model/download", json={"model": "qwen3-4b"}).json()
        drain_until_job(ws, job["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed" and done["result"] == {"model": "qwen3-4b"}
    status = client.get("/api/local-model").json()
    assert status["server_installed"]
    assert next(m for m in status["models"] if m["id"] == "qwen3-4b")["downloaded"]
    assert client.delete("/api/local-model/qwen3-4b").status_code == 200
    assert not local_llm.manager(ctx.settings.models_dir).downloaded()


def test_a_model_is_pulled_into_ollama_with_its_progress(client, ctx):
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling abc", "total": 100, "completed": 50},
        {"status": "success"},
    ]

    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        assert request.url.path == "/api/pull"
        assert json.loads(request.content)["model"] == "qwen3:4b-instruct-2507-q4_K_M"
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    ctx.http_transport = httpx.MockTransport(ollama)
    assert client.get("/api/local-model").json()["ollama"]["reachable"] is True
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        job = client.post("/api/ollama/pull", json={"model": "qwen3:4b-instruct-2507-q4_K_M"})
        events = drain_until_job(ws, job.json()["id"])
    progress = [e["job"]["progress"] for e in events if e.get("type") == "job"]
    assert 0.5 in progress
    assert client.get(f"/api/jobs/{job.json()['id']}").json()["status"] == "completed"


def test_downloads_refuse_a_file_that_does_not_match(tmp_path, monkeypatch):
    def stream(method, url, **kw):
        class R:
            status_code = 200
            headers = {"content-length": "4"}

            def iter_bytes(self, n):
                yield b"evil"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    monkeypatch.setattr(downloads.httpx, "stream", stream)
    item = downloads.Download(url="https://x/y", sha256="0" * 64, size=4, name="y.bin")
    with pytest.raises(downloads.DownloadError, match="checksum"):
        downloads.fetch(tmp_path, item)
    assert list(tmp_path.iterdir()) == []  # nothing left behind
