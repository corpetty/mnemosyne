"""The built-in summary model: summaries with nothing else installed (no Ollama, no account).

On first use the backend downloads llama.cpp's `llama-server` (its Vulkan build when the machine
has Vulkan, which also carries the CPU code; else the CPU build) and a GGUF model into
`<data_dir>/models/llm`, both pinned and checked by SHA-256 (services/downloads.py). The server
runs on 127.0.0.1 on a free port only while summaries need it: it starts on the first request and
stops after `unload_models_after_minutes` without one. llama.cpp fits the model into whatever GPU
memory is free and runs the rest on the CPU.

Models (Apache-2.0): Qwen3-4B-Instruct-2507 (2.5 GB) for most machines, Qwen3-30B-A3B-Instruct-2507
(18.6 GB; a mixture of experts, quick for its size) where there are 32 GB of RAM or more.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import socket
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .downloads import Download, ProgressFn, fetch

logger = logging.getLogger(__name__)

LLAMA_BUILD = "b11323"
_RELEASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}"
SERVERS = {
    "vulkan": Download(
        url=f"{_RELEASE}/llama-{LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz",
        sha256="db9d96194370ee2c7e7583e02a6d067ff8ebc66d7de8eaf1d863aa79ea32c948",
        size=31_492_304,
        name=f"llama-{LLAMA_BUILD}-vulkan.tar.gz",
    ),
    "cpu": Download(
        url=f"{_RELEASE}/llama-{LLAMA_BUILD}-bin-ubuntu-x64.tar.gz",
        sha256="6b8801b592f19d838a0c5e6bbf6282f8f0788d8c99561b98cc4cd3ef558346f5",
        size=17_544_991,
        name=f"llama-{LLAMA_BUILD}-cpu.tar.gz",
    ),
}


@dataclass(frozen=True)
class LocalModel:
    id: str  # what default_model holds
    label: str
    file: Download
    min_ram_gb: int  # offered as the recommendation from this much RAM
    context: int  # tokens of context the server is started with


_HF = "https://huggingface.co/unsloth"
MODELS = {
    m.id: m
    for m in (
        LocalModel(
            id="qwen3-4b",
            label="Qwen3 4B Instruct (2.5 GB)",
            file=Download(
                url=f"{_HF}/Qwen3-4B-Instruct-2507-GGUF/resolve/"
                "a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
                sha256="3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
                size=2_497_281_120,
                name="Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            ),
            min_ram_gb=0,
            context=16384,
        ),
        LocalModel(
            id="qwen3-30b-a3b",
            label="Qwen3 30B-A3B Instruct (18.6 GB)",
            file=Download(
                url=f"{_HF}/Qwen3-30B-A3B-Instruct-2507-GGUF/resolve/"
                "eea7b2be5805a5f151f8847ede8e5f9a9284bf77/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
                sha256="6c997b8af17debdfb01d890214400ccbab00db6acc0ba8da5de1cc906c4774d0",
                size=18_556_686_752,
                name="Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
            ),
            min_ram_gb=30,  # a "32 GB" computer reports about 31
            context=16384,
        ),
    )
}


def ram_gb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // (1024 * 1024)
    except OSError:
        pass
    return 0


def recommended() -> str:
    """The biggest model this machine has the memory for."""
    ram = ram_gb()
    return max((m for m in MODELS.values() if ram >= m.min_ram_gb), key=lambda m: m.min_ram_gb).id


def has_vulkan() -> bool:
    try:
        ctypes.CDLL("libvulkan.so.1")
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalLLM:
    """Downloads, starts and stops the built-in model's server. One per data folder."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._proc: subprocess.Popen | None = None
        self._model: str | None = None
        self._port = 0
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    # ---- what is here -----------------------------------------------------------------

    def server_dir(self) -> Path:
        return self.folder / "server" / LLAMA_BUILD

    def server_binary(self) -> Path:
        return self.server_dir() / "llama-server"

    def downloaded(self) -> list[str]:
        return [m.id for m in MODELS.values() if (self.folder / m.file.name).is_file()]

    def ready(self, model_id: str) -> bool:
        return self.server_binary().is_file() and model_id in self.downloaded()

    @property
    def running(self) -> str | None:
        """The model being served, if the server is up."""
        return self._model if self._proc is not None and self._proc.poll() is None else None

    # ---- downloads (blocking: run in a thread) ----------------------------------------

    def install(self, model_id: str, progress: ProgressFn | None = None) -> None:
        """The server and the model, downloaded and checked; nothing to do when they are here.
        `progress(done, total)` counts the bytes of both."""
        model = MODELS[model_id]
        server = SERVERS["vulkan" if has_vulkan() else "cpu"]
        need_server = not self.server_binary().is_file()
        total = (server.size if need_server else 0) + model.file.size
        offset = 0

        def report(done: int, _total: int) -> None:
            if progress:
                progress(offset + done, total)

        if need_server:
            archive = fetch(self.folder, server, report)
            self._unpack(archive)
            archive.unlink(missing_ok=True)
            offset = server.size
        fetch(self.folder, model.file, report)

    def _unpack(self, archive: Path) -> None:
        target = self.server_dir()
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                # The release keeps everything in one folder (llama-<build>/...): flatten it.
                name = Path(member.name).name
                if not (member.isfile() or member.issym()) or not name:
                    continue
                if member.issym():
                    link = target / name
                    link.unlink(missing_ok=True)
                    link.symlink_to(Path(member.linkname).name)
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                out = target / name
                with out.open("wb") as f:
                    while chunk := src.read(1 << 20):
                        f.write(chunk)
                out.chmod(0o755 if member.mode & 0o111 else 0o644)

    def remove(self, model_id: str) -> None:
        if self.running == model_id:
            self.stop()
        (self.folder / MODELS[model_id].file.name).unlink(missing_ok=True)

    # ---- the server -------------------------------------------------------------------

    def _discrete_gpu(self) -> str | None:
        """llama.cpp's name for an NVIDIA or AMD card, so an integrated GPU next to it is not
        given half the model; None to let llama.cpp choose."""
        try:
            out = subprocess.run(
                [str(self.server_binary()), "--list-devices"],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        devices = [line.strip() for line in out.splitlines() if line.strip().startswith("Vulkan")]
        for d in devices:
            if "NVIDIA" in d or "Radeon RX" in d:
                return d.split(":", 1)[0]
        return None

    async def url(self, model_id: str) -> str:
        """The base URL of a server running `model_id`, started (or restarted with another
        model) when needed."""
        async with self._lock:
            self._last_used = time.monotonic()
            if self.running == model_id:
                return f"http://127.0.0.1:{self._port}"
            if model_id not in self.downloaded():
                raise RuntimeError(
                    "The built-in model is not downloaded yet (Settings → AI → Built-in model)"
                )
            if not self.ready(model_id):  # the model is here, its small server is not
                await asyncio.to_thread(self.install, model_id)
            self.stop()
            await asyncio.to_thread(self._start, model_id)
            await self._wait_healthy()
            return f"http://127.0.0.1:{self._port}"

    def _start(self, model_id: str) -> None:
        model = MODELS[model_id]
        self._port = _free_port()
        args = [
            str(self.server_binary()),
            "--model",
            str(self.folder / model.file.name),
            "--host",
            "127.0.0.1",
            "--port",
            str(self._port),
            "--ctx-size",
            str(model.context),
            "--parallel",
            "1",
            "--no-webui",
        ]
        if device := self._discrete_gpu():
            args += ["--device", device]
        log = (self.folder / "server.log").open("ab")
        logger.info("Starting the built-in model %s on port %d", model_id, self._port)
        self._proc = subprocess.Popen(
            args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=os.environ
        )
        self._model = model_id

    async def _wait_healthy(self, timeout: float = 300.0) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=5) as client:
            while time.monotonic() < deadline:
                if self._proc is None or self._proc.poll() is not None:
                    raise RuntimeError(
                        "The built-in model's server stopped while starting; see "
                        f"{self.folder / 'server.log'}"
                    )
                try:
                    if (
                        await client.get(f"http://127.0.0.1:{self._port}/health")
                    ).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        self.stop()
        raise RuntimeError("The built-in model did not start in time")

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def stop(self) -> None:
        proc, self._proc, self._model = self._proc, None, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_if_idle(self, idle_seconds: float) -> bool:
        if self.running and idle_seconds > 0 and time.monotonic() - self._last_used > idle_seconds:
            logger.info("Stopping the built-in model after %.0f idle minutes", idle_seconds / 60)
            self.stop()
            return True
        return False


_managers: dict[Path, LocalLLM] = {}


def manager(models_dir: Path) -> LocalLLM:
    """The one LocalLLM for this data folder (providers are rebuilt with settings; it is not)."""
    folder = Path(models_dir) / "llm"
    if folder not in _managers:
        _managers[folder] = LocalLLM(folder)
    return _managers[folder]
