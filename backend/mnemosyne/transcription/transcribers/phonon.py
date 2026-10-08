"""Phonon-2 (Fermion Research), English only: Parakeet TDT 0.6B v3 in a 164 MB file with its own
CPU engine. Runs as `phonon serve`, an OpenAI-compatible server we start and stop.

Fermion's package is not a dependency of the backend: people install it themselves
(`pip install fermion-research`, see Settings) and we find `phonon` on PATH or at
`phonon_command`. On Corey's machine (2026-10-08) a 43-minute meeting took 48 s per source,
about 4x faster than Parakeet int8 on the system audio, with word timestamps on every segment
and 7 % of words differing from Parakeet's transcript, mostly fillers and spelling ("want to"
for "wanna"). Requests go through RemoteTranscriber, so segments and word times are parsed the
same way as for any remote server.

One server per command is shared by every PhononTranscriber (final and live transcription);
it stops when the last one unloads, and dies with the backend (PR_SET_PDEATHSIG).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from ...models.transcript import TranscriptSegment
from .remote import RemoteTranscriber

logger = logging.getLogger(__name__)

MODEL = "FermionResearch/Phonon-2"
INSTALL_HINT = (
    "Phonon is not installed: install it with `pip install fermion-research` (its CPU engine "
    "also needs CPU PyTorch; see fermionresearch.com/docs/speech), or set its path in "
    "Settings → Transcription"
)


def find_phonon(command: str = "") -> str | None:
    """The `phonon` program to run: `command` when it is set (a path or a name on PATH),
    else `phonon` on PATH. None when there is none."""
    if command:
        path = Path(command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return shutil.which(command)
    return shutil.which("phonon")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _die_with_parent() -> None:
    """In the child, before exec: get SIGTERM when the backend dies (Linux)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class PhononServer:
    """`phonon serve` on a free local port, started on first use."""

    def __init__(self, program: str, log_path: Path | None = None):
        self.program = program
        self.log_path = log_path
        self.port = 0
        self._proc: subprocess.Popen | None = None
        self._users = 0
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def acquire(self, timeout: float = 600.0) -> str:
        """Start the server if needed (the first start downloads the model) and count a user.
        Returns its base URL."""
        async with self._lock:
            if not self.running():
                await asyncio.to_thread(self._start)
                await self._wait_healthy(timeout)
            self._users += 1
            return self.url

    async def release(self) -> None:
        async with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0:
                await asyncio.to_thread(self.stop)

    def _start(self) -> None:
        self.port = _free_port()
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab") if self.log_path else subprocess.DEVNULL
        logger.info("Starting phonon serve on port %d", self.port)
        self._proc = subprocess.Popen(
            [self.program, "serve", "--port", str(self.port)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )

    async def _wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=5) as client:
            while time.monotonic() < deadline:
                if not self.running():
                    where = f"; see {self.log_path}" if self.log_path else ""
                    raise RuntimeError(f"phonon serve stopped while starting{where}")
                try:
                    if (await client.get(f"{self.url}/health")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        self.stop()
        raise RuntimeError("phonon serve did not start in time")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            logger.info("Stopping phonon serve")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


_servers: dict[str, PhononServer] = {}


def server_for(program: str, log_path: Path | None = None) -> PhononServer:
    if program not in _servers:
        _servers[program] = PhononServer(program, log_path)
    return _servers[program]


class PhononTranscriber:
    name = "phonon"

    def __init__(self, command: str = "", log_path: Path | None = None, model: str = MODEL):
        self.command = command
        self.log_path = log_path
        self.model = model
        self._server: PhononServer | None = None
        self._client: RemoteTranscriber | None = None

    def is_loaded(self) -> bool:
        return self._client is not None and self._server is not None and self._server.running()

    async def load(self) -> None:
        if self.is_loaded():
            return
        if self._server is not None:  # its server died: let go of it before starting again
            await self.unload()
        program = find_phonon(self.command)
        if program is None:
            raise RuntimeError(INSTALL_HINT)
        server = server_for(program, self.log_path)
        url = await server.acquire()
        self._server = server
        # Long meetings: the server answers when the whole file is transcribed.
        self._client = RemoteTranscriber(f"{url}/v1", model=self.model, timeout=3600.0)

    async def unload(self) -> None:
        server, self._server, self._client = self._server, None, None
        if server is not None:
            await server.release()

    async def transcribe(
        self, audio_path: str, language: str | None = None, progress=None
    ) -> list[TranscriptSegment]:
        if language and not language.lower().startswith("en"):
            raise ValueError("Phonon-2 transcribes English only; choose another transcriber")
        if not self.is_loaded():
            await self.load()
        assert self._client is not None
        segments = await self._client.transcribe(audio_path)
        if progress is not None:
            progress(1.0)
        return segments
