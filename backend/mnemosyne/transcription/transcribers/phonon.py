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
import hashlib
import logging
import os
import secrets
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
# The model files Phonon runs, pinned by SHA-256 (the files on Corey's machine, 2026-10-12, which
# Fermion itself verified against the hashes in its wheel: the archive "verified against the pinned
# sha256 (98125795b6dd…)", every member of it after unpacking). Paths are relative to our own
# install's cache folder (`<data_dir>/phonon/cache`, FERMION_CACHE_DIR). The archive is what is
# downloaded, `model.fermion` what the engine loads: checked again here so that a file replaced
# after the install is not run.
MODEL_FILES = {
    "models--FermionResearch--Phonon-2/snapshots/*/phonon-2.bps.tar.zst": (
        "98125795b6dda72f5c6eee9ba33d19815df65dcb18b50a357bf9f73c9935309e"
    ),
    "speech/FermionResearch__Phonon-2/*/model.fermion": (
        "4b6bfa3a12cc3c4e0a54f2ab3ec4ca7a842b09e5c7ecfc8e7ca0ac6cc8c11468"
    ),
}
INSTALL_HINT = (
    "Phonon is not installed: install it with `pip install fermion-research` (its CPU engine "
    "also needs CPU PyTorch; see fermionresearch.com/docs/speech), or set its path in "
    "Settings → Transcription"
)


def managed_program(home: Path) -> Path:
    """`phonon` as Mnemosyne installs it (services/phonon_install.py)."""
    return home / "venv" / "bin" / "phonon"


class ModelMismatch(RuntimeError):
    """The model file is missing or is not the one pinned in MODEL_FILES."""


# (path, size, mtime_ns) -> sha256 of files already hashed in this process.
_hashed: dict[tuple[str, int, int], str] = {}
# Installs whose model did not match, with why: not offered as `phonon` until installed again.
_refused: dict[Path, str] = {}


def _sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hashed:
        with path.open("rb") as f:
            _hashed[key] = hashlib.file_digest(f, "sha256").hexdigest()
    return _hashed[key]


def verify_model(home: Path) -> None:
    """Check the model files under `home` (our own install) against MODEL_FILES. Raises
    ModelMismatch, saying which file and what was found; the install is then refused (see
    `find_phonon`) until it is installed again. A file is hashed once per (path, size, mtime)."""
    cache = home / "cache"
    for pattern, expected in MODEL_FILES.items():
        name = pattern.rsplit("/", 1)[-1]
        found = sorted(cache.glob(pattern))
        if not found:
            message = (
                f"Phonon-2's model file {name} is missing from {cache}; not starting Phonon. "
                "Install Phonon again in Settings → Transcription"
            )
            _refused[home] = message
            raise ModelMismatch(message)
        for path in found:
            actual = _sha256(path)
            if actual != expected:
                message = (
                    f"Phonon-2's model file {name} does not match its pinned SHA-256 "
                    f"(expected {expected[:12]}…, found {actual[:12]}…); not starting Phonon. "
                    "Install Phonon again in Settings → Transcription, or use another transcriber"
                )
                _refused[home] = message
                raise ModelMismatch(message)
    _refused.pop(home, None)


def find_phonon(command: str = "", home: Path | None = None) -> str | None:
    """The `phonon` program to run: `command` when it is set (a path or a name on PATH), else
    our own install under `home` once it is complete, else `phonon` on PATH. None: there is none."""
    if command:
        path = Path(command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return shutil.which(command)
    if (
        home is not None
        and home not in _refused
        and (home / "installed").exists()
        and managed_program(home).is_file()
    ):
        return str(managed_program(home))
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


# Variables that would make Fermion load or run other native code, unchecked (review of
# fermion-research 0.2.11, 2026-10-08): never passed on to the server.
_UNSAFE_ENV = ("PHONON", "FERMION_")


def server_env(home: Path | None) -> dict[str, str]:
    """The server's environment: ours without Fermion's overrides; with `home` (our own install,
    services/phonon_install.py) its caches there and no network once the model is installed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_UNSAFE_ENV)}
    if home is not None:
        env["FERMION_CACHE_DIR"] = str(home / "cache")
        env["HF_HUB_CACHE"] = str(home / "hf")
        if (home / "installed").exists():
            env["HF_HUB_OFFLINE"] = "1"
    return env


class PhononServer:
    """`phonon serve` on a free local port, started on first use. Loopback only (Fermion's
    default host, never --host), with a random API key per start: nothing else on the machine
    gets to use it (it has no host or origin check)."""

    def __init__(
        self,
        program: str,
        log_path: Path | None = None,
        threads: int | None = None,
        home: Path | None = None,
    ):
        self.program = program
        self.log_path = log_path
        self.threads = threads
        self.home = home
        self.api_key = ""
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
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Our own finished install: its model must be the pinned one before it is run. (The first
        # start, inside the install, has no `installed` yet and downloads it; the install checks
        # it afterwards.)
        if self.home is not None and (self.home / "installed").exists():
            try:
                verify_model(self.home)
            except ModelMismatch as e:
                logger.error("%s", e)
                if self.log_path:
                    with self.log_path.open("a") as f:
                        f.write(f"[mnemosyne] {e}\n")
                raise
        self.port = _free_port()
        log = self.log_path.open("ab") if self.log_path else subprocess.DEVNULL
        logger.info("Starting phonon serve on port %d", self.port)
        # hex, not urlsafe base64: a key that starts with "-" is taken for an option and the server
        # does not start (1 in 64 starts)
        self.api_key = secrets.token_hex(24)
        # `phonon serve` names its model itself: a "phonon-2" argument is refused (0.16.0's
        # install failed at its first start on that, 2026-10-12).
        args = [
            self.program,
            "serve",
            "--port",
            str(self.port),
            "--api-key",
            self.api_key,
        ]
        if self.threads:
            args += ["--threads", str(self.threads)]
        self._proc = subprocess.Popen(
            args,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=server_env(self.home),
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


def server_for(
    program: str, log_path: Path | None = None, threads: int | None = None, home: Path | None = None
):
    key = (program, threads)
    if key not in _servers:
        _servers[key] = PhononServer(program, log_path, threads, home)
    return _servers[key]


class PhononTranscriber:
    name = "phonon"

    def __init__(
        self,
        command: str = "",
        log_path: Path | None = None,
        model: str = MODEL,
        threads: int | None = None,
        home: Path | None = None,
    ):
        self.command = command
        self.home = home  # <data_dir>/phonon: our own install, when there is one
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
        program = find_phonon(self.command, self.home)
        if program is None:
            if self.home is not None and not self.command and self.home in _refused:
                raise ModelMismatch(_refused[self.home])
            raise RuntimeError(INSTALL_HINT)
        home = self.home if self.home and program == str(managed_program(self.home)) else None
        server = server_for(program, self.log_path, self.threads, home)
        url = await server.acquire()
        self._server = server
        self._client = RemoteTranscriber(
            f"{url}/v1", model=self.model, api_key=server.api_key, timeout=600.0
        )

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
