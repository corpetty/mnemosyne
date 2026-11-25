"""Installing a narration engine (synthesis/workers.py `EngineSpec`: qwen3.py, kitten.py).

Like Phonon (services/phonon_install.py, whose steps this follows), an engine needs its own
torch and transformers, so it gets its own environment, `<data_dir>/tts/<engine>/venv`, made by uv
from the backend's own Python with every package at the exact version and SHA-256 in the
platform's requirements file (synthesis/requirements/<engine>-<platform>.txt, --require-hashes):
`macos` on a Mac; on Linux `linux-cuda` when an NVIDIA driver answers (`nvidia-smi`; CUDA 12.8,
the backend's gpu extra's baseline: 3.8x faster for KittenTTS in Task 0), else `linux` with
PyTorch's CPU build. On Linux torch comes first, alone, from PyTorch's index (`--no-deps`), then
the rest.

Then the model is downloaded once at its pinned revision into `<home>/model` (the worker's
`--prefetch`, with what the engine loads besides and AudioSeal's generator and detector, each at
its pinned revision, into `<home>/cache`). Every downloaded file's SHA-256 goes into `installed`
(JSON) and `verify` checks them again; from then on the worker runs with HF_HUB_OFFLINE=1.
Removing the folder removes all of it.

An engine whose weights are not open source (`EngineSpec.open_license` False: KittenTTS) is
installed only once the person accepted its licence, and `installed` keeps when they did.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..synthesis import kitten, qwen3
from ..synthesis.workers import AUDIOSEAL_REVISION, WORKER, EngineSpec, python_of, worker_env

logger = logging.getLogger(__name__)

ENGINES: dict[str, EngineSpec] = {s.name: s for s in (qwen3.SPEC, kitten.SPEC)}
TORCH_INDEX = {
    "linux": "https://download.pytorch.org/whl/cpu",
    "linux-cuda": "https://download.pytorch.org/whl/cu128",
}

Report = Callable[[str, float], None]  # message, fraction done


class LicenseNotAccepted(ValueError):
    pass


class ModelMismatch(RuntimeError):
    pass


def on_mac() -> bool:
    return sys.platform == "darwin"


def has_nvidia() -> bool:
    """An NVIDIA driver answers (the backend's first-launch install asks the same way)."""
    try:
        return subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def platform(spec: EngineSpec) -> str:
    if on_mac():
        return "macos"
    return "linux-cuda" if spec.cuda and has_nvidia() else "linux"


def requirements_hash(spec: EngineSpec, plat: str) -> str:
    return hashlib.sha256(spec.requirements(plat).read_bytes()).hexdigest()[:16]


def stamp(spec: EngineSpec, data_dir: Path) -> dict | None:
    try:
        return json.loads((spec.home(data_dir) / "installed").read_text())
    except (OSError, ValueError):
        return None


def installed(spec: EngineSpec, data_dir: Path) -> bool:
    """Installed from requirements and a model revision this backend carries."""
    s = stamp(spec, data_dir)
    if s is None or s.get("revision") != spec.revision:
        return False
    plat = s.get("platform", "")
    try:
        same = s.get("requirements") == requirements_hash(spec, plat)
    except OSError:
        return False
    return same and python_of(spec.home(data_dir)).exists()


def size_bytes(spec: EngineSpec, data_dir: Path) -> int:
    root = spec.home(data_dir)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file() and not f.is_symlink())


def uv_binary() -> str | None:
    return os.environ.get("MNEMOSYNE_UV_BIN") or shutil.which("uv")


def _pin(name: str, file: Path) -> str:
    """The requirement `name==version` with its `--hash` lines, as a requirements file's text."""
    lines: list[str] = []
    for line in file.read_text().splitlines():
        if lines:
            if line[:1].isspace():
                lines.append(line)
                continue
            break
        if line.lower().startswith(f"{name}=="):
            lines.append(line)
    if not lines or not any("--hash=" in line for line in lines):
        raise ValueError(f"{name} is not pinned with hashes in {file.name}")
    return "\n".join(lines) + "\n"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def model_files(root: Path) -> list[Path]:
    """What the model is made of: the pinned copy and the HF cache's blobs (vocoder, AudioSeal)."""
    files = []
    for top in (root / "model", root / "cache"):
        if top.exists():
            files += [p for p in top.rglob("*") if p.is_file() and not p.is_symlink()]
    skip = (".cache", "hf-home")
    return sorted(
        f for f in files if not any(s in f.parts for s in skip) and not f.name.endswith(".lock")
    )


def manifest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha256(p) for p in model_files(root)}


def verify(spec: EngineSpec, data_dir: Path) -> None:
    """Raise ModelMismatch when a model file differs from what the install recorded."""
    root = spec.home(data_dir)
    recorded = (stamp(spec, data_dir) or {}).get("files") or {}
    if not recorded:
        raise ModelMismatch("No record of the model's files: install again")
    for rel, digest in recorded.items():
        path = root / rel
        if not path.is_file() or _sha256(path) != digest:
            raise ModelMismatch(f"{rel} is not the file that was installed")


async def _run(args: list[str], report: Report, done: float, env: dict[str, str]) -> None:
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
        raise RuntimeError("Installing the voice engine failed: " + " | ".join(tail[-5:]))


async def install(
    spec: EngineSpec,
    data_dir: Path,
    report: Report,
    accepted_license: bool,
    uv: str | None = None,
    weights: str = "",
    plat: str | None = None,
) -> None:
    """Install (or bring up to date) `spec` under <data_dir>/tts/<name>. A failed install leaves
    no `installed` stamp, so it is not used."""
    if not spec.open_license and not accepted_license:
        raise LicenseNotAccepted(
            f"{spec.title}'s weights are under the {spec.license_name}: accept it to install"
        )
    uv = uv or uv_binary()
    if uv is None:
        raise RuntimeError("Installing a voice engine needs uv, which was not found")
    plat = plat or platform(spec)
    reqs = spec.requirements(plat)
    root = spec.home(data_dir)
    python = python_of(root)
    (root / "installed").unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_PROGRESS": "1",
        "VIRTUAL_ENV": "",
    }
    if not python.exists():
        report(f"Making {spec.title}'s environment", 0.02)
        await _run([uv, "venv", "--python", sys.executable, str(root / "venv")], report, 0.02, env)
    index = TORCH_INDEX.get(plat)
    if index:
        what = (
            "for NVIDIA GPUs (about 3 GB)" if plat == "linux-cuda" else "for the CPU (about 700 MB)"
        )
        report(f"Installing PyTorch {what}", 0.05)
        torch_file = root / "torch-requirements.txt"
        torch_file.write_text(_pin("torch", reqs))
        await _run(
            [uv, "pip", "install", "--python", str(python), "--require-hashes", "--no-deps"]
            + ["--index-url", index, "-r", str(torch_file)],
            report,
            0.3,
            env,
        )
    report(f"Installing {spec.title}", 0.35)
    await _run(
        [uv, "pip", "install", "--python", str(python), "--require-hashes"]
        + (["--extra-index-url", index, "--index-strategy", "unsafe-best-match"] if index else [])
        + ["-r", str(reqs)],
        report,
        0.6,
        env,
    )
    report(f"Downloading the {spec.title} model", 0.65)
    prefetch = [str(python), "-I", str(WORKER), "--engine", spec.name, "--prefetch"]
    prefetch += ["--model", str(root / "model"), "--repo", spec.repo, "--revision", spec.revision]
    prefetch += [
        "--vocoder-revision",
        spec.extra_revision,
        "--audioseal-revision",
        AUDIOSEAL_REVISION,
    ]
    prefetch += ["--weights", weights]
    await _run(prefetch, report, 0.9, worker_env(root, offline=False))
    report("Recording the model's checksums", 0.95)
    files = await asyncio.to_thread(manifest, root)
    (root / "installed").write_text(
        json.dumps(
            {
                "engine": spec.name,
                "platform": plat,
                "requirements": requirements_hash(spec, plat),
                "revision": spec.revision,
                "extra_revision": spec.extra_revision,
                "audioseal_revision": AUDIOSEAL_REVISION,
                "weights": weights,  # KittenTTS: a different `kitten_weights` installs again
                "license": spec.license_name,
                "license_accepted_at": (
                    None if spec.open_license else datetime.now().isoformat(timespec="seconds")
                ),
                "files": files,
            },
            indent=1,
        )
    )
    report(f"{spec.title} is installed", 1.0)
    logger.info("%s installed in %s (%.0f MB)", spec.title, root, size_bytes(spec, data_dir) / 1e6)


def remove(spec: EngineSpec, data_dir: Path) -> None:
    shutil.rmtree(spec.home(data_dir), ignore_errors=True)
