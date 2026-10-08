"""Phonon-2 (Fermion Research), English only: Parakeet TDT 0.6B v3 in a 164 MB file with its own
CPU engine. Runs as `phonon serve`, an OpenAI-compatible server we start and stop.

Fermion's package is not a dependency of the backend: people install it themselves
(`pip install fermion-research`, see Settings) and we find `phonon` on PATH or at
`phonon_command`. On Corey's machine (2026-10-08) a 43-minute meeting took 48 s per source,
about 4x faster than Parakeet int8 on the system audio, with word timestamps on every segment
and 7 % of words differing from Parakeet's transcript, mostly fillers and spelling ("want to"
for "wanna"). Requests go through RemoteTranscriber, so segments and word times are parsed the
same way as for any remote server.

One server per (command, threads) is shared by the transcribers that use it; it stops when the
last one unloads, and dies with the backend (PR_SET_PDEATHSIG). Live transcription passes its
small thread budget (`live_threads`, `--threads`), so it gets a server of its own.

The server refuses large uploads (413 for a 33 MB WAV), so audio goes in pieces of about five
minutes, cut at the quietest moment before each boundary so no word is split; pieces that are
silence throughout (the echo-cancelled mic, mostly) are not sent, and progress follows the pieces.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from ...audio.mixer import decode_audio
from ...models.transcript import TranscriptSegment
from .remote import RemoteTranscriber

logger = logging.getLogger(__name__)

MODEL = "FermionResearch/Phonon-2"
SAMPLE_RATE = 16000
PIECE_SECONDS = 300.0  # 9.6 MB as 16 kHz 16-bit WAV
SEARCH_SECONDS = 15.0  # where to look for a quiet moment before each boundary
SILENT_DBFS = -55.0  # a piece whose loudest 100 ms stays below this is not sent"
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


def cut_points(pcm: np.ndarray, rate: int = SAMPLE_RATE, piece: float = PIECE_SECONDS) -> list[int]:
    """Sample indices that cut `pcm` into pieces of at most `piece` seconds, each cut at the
    quietest 100 ms of the `SEARCH_SECONDS` before its boundary. First 0, last len(pcm)."""
    win = rate // 10
    cuts = [0]
    while pcm.size - cuts[-1] > piece * rate:
        target = cuts[-1] + int(piece * rate)
        lo = max(cuts[-1] + win, target - int(SEARCH_SECONDS * rate))
        frames = pcm[lo:target][: ((target - lo) // win) * win].reshape(-1, win)
        quietest = int((frames.astype(np.float64) ** 2).mean(axis=1).argmin())
        cuts.append(lo + quietest * win + win // 2)
    cuts.append(int(pcm.size))
    return cuts


def is_silent(pcm: np.ndarray, rate: int = SAMPLE_RATE) -> bool:
    """Nothing in it reaches SILENT_DBFS over 100 ms."""
    win = rate // 10
    if pcm.size < win:
        return True
    frames = pcm[: (pcm.size // win) * win].reshape(-1, win).astype(np.float64)
    loudest = float(np.sqrt((frames**2).mean(axis=1)).max())
    return loudest <= 0 or 20 * np.log10(loudest) < SILENT_DBFS


def _write_wav(path: Path, pcm: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(pcm, -1.0, 1.0) * 32767).astype("<i2").tobytes())


def _shifted(segment: TranscriptSegment, by: float) -> TranscriptSegment:
    words = [
        w.model_copy(update={"start": w.start + by, "end": w.end + by}) for w in segment.words or []
    ]
    return segment.model_copy(
        update={"start": segment.start + by, "end": segment.end + by, "words": words or None}
    )


class PhononServer:
    """`phonon serve` on a free local port, started on first use."""

    def __init__(self, program: str, log_path: Path | None = None, threads: int | None = None):
        self.program = program
        self.log_path = log_path
        self.threads = threads
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
        args = [self.program, "serve", "--port", str(self.port)]
        if self.threads:
            args += ["--threads", str(self.threads)]
        self._proc = subprocess.Popen(
            args,
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


_servers: dict[tuple[str, int | None], PhononServer] = {}


def server_for(program: str, log_path: Path | None = None, threads: int | None = None):
    key = (program, threads)
    if key not in _servers:
        _servers[key] = PhononServer(program, log_path, threads)
    return _servers[key]


class PhononTranscriber:
    name = "phonon"

    def __init__(
        self,
        command: str = "",
        log_path: Path | None = None,
        model: str = MODEL,
        threads: int | None = None,
    ):
        self.command = command
        self.log_path = log_path
        self.model = model
        self.threads = threads  # CPU threads for its server; None: Fermion's default
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
        server = server_for(program, self.log_path, self.threads)
        url = await server.acquire()
        self._server = server
        self._client = RemoteTranscriber(f"{url}/v1", model=self.model, timeout=600.0)

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
        pcm = await asyncio.to_thread(decode_audio, Path(audio_path), SAMPLE_RATE)
        cuts = cut_points(pcm)
        out: list[TranscriptSegment] = []
        with tempfile.TemporaryDirectory(prefix="mnemosyne-phonon-") as tmp:
            piece_path = Path(tmp) / "piece.wav"
            for n, (a, b) in enumerate(zip(cuts, cuts[1:], strict=False)):
                piece = pcm[a:b]
                if not is_silent(piece):
                    await asyncio.to_thread(_write_wav, piece_path, piece)
                    offset = a / SAMPLE_RATE
                    out += [
                        _shifted(s, offset) for s in await self._client.transcribe(str(piece_path))
                    ]
                if progress is not None:
                    progress((n + 1) / (len(cuts) - 1))
        return [s for s in out if s.text]
