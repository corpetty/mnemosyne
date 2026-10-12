"""Installing Phonon-2 (transcription/transcribers/phonon.py) for people who choose it.

Fermion's fermion-research package needs torch and transformers 5, which do not belong in the
backend's environment (the GPU extra resolves transformers 4.57, and CPU-only installs have no
torch at all), so it gets its own: `<data_dir>/phonon/venv`, made by uv from the backend's own
Python, with every package at the exact version in phonon-requirements.txt. torch comes first,
alone, from PyTorch's CPU index (from PyPI it would be the multi-GB CUDA build); the rest from
PyPI. Every requirement carries the SHA-256 of its files (phonon-requirements.txt) and uv runs
with --require-hashes; torch's step is `--no-deps` (its dependencies are in the file and come in
the second step, which finds torch installed). Then the model is downloaded once, as its own step
(into `<data_dir>/phonon/cache`), checked against its pinned SHA-256 (phonon.MODEL_FILES; a
mismatch fails the install and empties the cache), and `installed` is written: from then on the
server runs with HF_HUB_OFFLINE=1, and checks the model again before each start. About 1 GB in
all; removing the folder removes all of it.

uv: the app's bundled one (MNEMOSYNE_UV_BIN, set by the desktop shell), else `uv` on PATH.
Reviewed 2026-10-08 (fermion-research 0.2.11): no telemetry; model archive pinned by sha256 in
the wheel; the native kernels are prebuilt with no published build provenance.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from ..transcription.transcribers import phonon

logger = logging.getLogger(__name__)

REQUIREMENTS = Path(phonon.__file__).with_name("phonon-requirements.txt")
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

Report = Callable[[str, float], None]  # message, fraction done


def home(data_dir: Path) -> Path:
    return Path(data_dir) / "phonon"


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:16]


def installed(data_dir: Path) -> bool:
    """Installed, from the requirements this backend carries (a newer pin set reinstalls)."""
    stamp = home(data_dir) / "installed"
    return (
        stamp.exists()
        and stamp.read_text().strip() == requirements_hash()
        and phonon.managed_program(home(data_dir)).is_file()
    )


def size_bytes(data_dir: Path) -> int:
    root = home(data_dir)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.is_symlink())


def uv_binary() -> str | None:
    return os.environ.get("MNEMOSYNE_UV_BIN") or shutil.which("uv")


def _pin(name: str) -> str:
    """The requirement `name==version` with its `--hash` lines, as a requirements file's text."""
    lines: list[str] = []
    for line in REQUIREMENTS.read_text().splitlines():
        if lines:  # inside the entry: its hashes are indented continuation lines
            if line[:1].isspace():
                lines.append(line)
                continue
            break
        if line.lower().startswith(f"{name}=="):
            lines.append(line)
    if not lines or not any("--hash=" in line for line in lines):
        raise ValueError(f"{name} is not pinned with hashes in {REQUIREMENTS.name}")
    return "\n".join(lines) + "\n"


async def _run(args: list[str], report: Report, done: float, env: dict[str, str]) -> None:
    """Run a uv step, passing its output lines on as progress messages."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
    )
    assert proc.stdout is not None
    tail: list[str] = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if line:
            tail = (tail + [line])[-20:]
            report(line[:200], done)
    if await proc.wait() != 0:
        raise RuntimeError("Installing Phonon failed: " + " | ".join(tail[-5:]))


async def install(data_dir: Path, report: Report, uv: str | None = None) -> None:
    """Install (or bring up to date) Phonon under <data_dir>/phonon. Raises with uv's last lines
    on failure; a failed install leaves no `installed` stamp, so it is not used."""
    uv = uv or uv_binary()
    if uv is None:
        raise RuntimeError("Installing Phonon needs uv, which was not found")
    root = home(data_dir)
    venv = root / "venv"
    python = venv / "bin" / "python"
    (root / "installed").unlink(missing_ok=True)
    phonon._refused.pop(root, None)  # installing again is the way out of a refusal
    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "UV_PYTHON_DOWNLOADS": "never",  # the backend's own Python, no other
        "UV_NO_PROGRESS": "1",
        "VIRTUAL_ENV": "",
    }
    if not python.exists():
        report("Making Phonon's environment", 0.02)
        await _run([uv, "venv", "--python", sys.executable, str(venv)], report, 0.02, env)
    report("Installing PyTorch for the CPU (about 700 MB)", 0.05)
    torch_file = root / "torch-requirements.txt"
    torch_file.write_text(_pin("torch"))
    await _run(
        [uv, "pip", "install", "--python", str(python), "--require-hashes", "--no-deps"]
        + ["--index-url", TORCH_INDEX, "-r", str(torch_file)],
        report,
        0.4,
        env,
    )
    report("Installing Fermion's Phonon package", 0.5)
    await _run(
        [uv, "pip", "install", "--python", str(python), "--require-hashes"]
        + ["-r", str(REQUIREMENTS)],
        report,
        0.7,
        env,
    )
    if (root / "cache").exists():  # an earlier download that is not the pinned one is not reused
        try:
            await asyncio.to_thread(phonon.verify_model, root)
        except phonon.ModelMismatch:
            shutil.rmtree(root / "cache", ignore_errors=True)
    report("Downloading the Phonon-2 model (164 MB)", 0.75)
    server = phonon.PhononServer(
        str(phonon.managed_program(root)),
        log_path=Path(data_dir) / "logs" / "phonon.log",
        home=root,  # no `installed` yet: this start may download
    )
    try:
        await server.acquire(timeout=1800.0)
    finally:
        await asyncio.to_thread(server.stop)
    report("Checking the model against its pinned checksum", 0.95)
    try:
        await asyncio.to_thread(phonon.verify_model, root)
    except phonon.ModelMismatch as e:
        logger.error("%s", e)
        log = Path(data_dir) / "logs" / "phonon.log"
        with log.open("a") as f:
            f.write(f"[mnemosyne] {e}\n")
        shutil.rmtree(root / "cache", ignore_errors=True)  # a retry downloads it again
        raise
    (root / "installed").write_text(requirements_hash() + "\n")
    report("Phonon-2 is installed", 1.0)
    logger.info("Phonon installed in %s (%.0f MB)", root, size_bytes(data_dir) / 1e6)


def remove(data_dir: Path) -> None:
    """Remove the whole install (environment, model, caches). Running servers are stopped."""
    for server in list(phonon._servers.values()):
        if server.home == home(data_dir):
            server.stop()
    phonon._refused.pop(home(data_dir), None)
    shutil.rmtree(home(data_dir), ignore_errors=True)
