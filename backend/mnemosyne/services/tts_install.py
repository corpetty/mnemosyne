"""Installing KittenTTS 2 (synthesis/kitten.py) for people who choose narration voices.

Like Phonon (services/phonon_install.py, whose steps this follows), it needs torch and
transformers 5, so it gets its own environment, `<data_dir>/tts/kitten/venv`, made by uv from the
backend's own Python with every package at the exact version and SHA-256 in the platform's
requirements file (synthesis/kitten-requirements.txt, -macos.txt; --require-hashes). On Linux torch
comes first, alone, from PyTorch's CPU index (`--no-deps`), then the rest from PyPI.

Then the model is downloaded once at its pinned revision into `<home>/model` (the worker's
`--prefetch`: only the files of the chosen weight variant, then the Chatterbox-Turbo vocoder and
AudioSeal's generator and detector, each at its pinned revision, into `<home>/cache`).
Every downloaded file's SHA-256 goes into `installed` (JSON) and `verify` checks them again; from
then on the worker runs with HF_HUB_OFFLINE=1. About 3 GB in all; removing the folder removes it.

The model's weights are not open source (kitten.py, LICENSE_NAME): `install` refuses unless the
person accepted that licence, and `installed` keeps when they did.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..synthesis import kitten

logger = logging.getLogger(__name__)

LINUX_REQUIREMENTS = Path(kitten.__file__).with_name("kitten-requirements.txt")
MAC_REQUIREMENTS = Path(kitten.__file__).with_name("kitten-requirements-macos.txt")
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

Report = Callable[[str, float], None]  # message, fraction done


class LicenseNotAccepted(ValueError):
    pass


class ModelMismatch(RuntimeError):
    pass


def on_mac() -> bool:
    return sys.platform == "darwin"


def requirements() -> Path:
    return MAC_REQUIREMENTS if on_mac() else LINUX_REQUIREMENTS


def requirements_hash() -> str:
    return hashlib.sha256(requirements().read_bytes()).hexdigest()[:16]


def stamp(data_dir: Path) -> dict | None:
    try:
        return json.loads((kitten.home(data_dir) / "installed").read_text())
    except (OSError, ValueError):
        return None


def installed(data_dir: Path) -> bool:
    """Installed from the requirements and model revision this backend carries."""
    s = stamp(data_dir)
    return (
        s is not None
        and s.get("requirements") == requirements_hash()
        and s.get("revision") == kitten.MODEL_REVISION
        and kitten.python_of(kitten.home(data_dir)).exists()
    )


def size_bytes(data_dir: Path) -> int:
    root = kitten.home(data_dir)
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
    return sorted(f for f in files if ".cache" not in f.parts and not f.name.endswith(".lock"))


def manifest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha256(p) for p in model_files(root)}


def verify(data_dir: Path) -> None:
    """Raise ModelMismatch when a model file differs from what the install recorded."""
    root = kitten.home(data_dir)
    recorded = (stamp(data_dir) or {}).get("files") or {}
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
        raise RuntimeError("Installing KittenTTS failed: " + " | ".join(tail[-5:]))


async def install(
    data_dir: Path,
    report: Report,
    accepted_license: bool,
    uv: str | None = None,
    weights: str = "",
) -> None:
    """Install (or bring up to date) KittenTTS under <data_dir>/tts/kitten. A failed install
    leaves no `installed` stamp, so it is not used."""
    if not accepted_license:
        raise LicenseNotAccepted(
            f"KittenTTS 2's weights are under the {kitten.LICENSE_NAME}: accept it to install"
        )
    uv = uv or uv_binary()
    if uv is None:
        raise RuntimeError("Installing KittenTTS needs uv, which was not found")
    root = kitten.home(data_dir)
    python = kitten.python_of(root)
    (root / "installed").unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_PROGRESS": "1",
        "VIRTUAL_ENV": "",
    }
    if not python.exists():
        report("Making the voice engine's environment", 0.02)
        await _run([uv, "venv", "--python", sys.executable, str(root / "venv")], report, 0.02, env)
    if not on_mac():
        report("Installing PyTorch for the CPU (about 700 MB)", 0.05)
        torch_file = root / "torch-requirements.txt"
        torch_file.write_text(_pin("torch", requirements()))
        await _run(
            [uv, "pip", "install", "--python", str(python), "--require-hashes", "--no-deps"]
            + ["--index-url", TORCH_INDEX, "-r", str(torch_file)],
            report,
            0.3,
            env,
        )
    report("Installing KittenTTS", 0.35)
    await _run(
        [uv, "pip", "install", "--python", str(python), "--require-hashes"]
        + (["--extra-index-url", TORCH_INDEX] if not on_mac() else [])
        + ["-r", str(requirements())],
        report,
        0.6,
        env,
    )
    report("Downloading the KittenTTS 2 model (about 1.5 GB)", 0.65)
    await _run(
        [str(python), "-I", str(kitten.WORKER), "--prefetch", "--model", str(root / "model")]
        + ["--revision", kitten.MODEL_REVISION, "--vocoder-revision", kitten.VOCODER_REVISION]
        + ["--audioseal-revision", kitten.AUDIOSEAL_REVISION, "--weights", weights],
        report,
        0.9,
        kitten.worker_env(root, offline=False),
    )
    report("Recording the model's checksums", 0.95)
    files = await asyncio.to_thread(manifest, root)
    (root / "installed").write_text(
        json.dumps(
            {
                "requirements": requirements_hash(),
                "revision": kitten.MODEL_REVISION,
                "vocoder_revision": kitten.VOCODER_REVISION,
                "audioseal_revision": kitten.AUDIOSEAL_REVISION,
                "weights": weights,  # a different `kitten_weights` needs installing again
                "license": kitten.LICENSE_NAME,
                "license_accepted_at": datetime.now().isoformat(timespec="seconds"),
                "files": files,
            },
            indent=1,
        )
    )
    report("KittenTTS is installed", 1.0)
    logger.info("KittenTTS installed in %s (%.0f MB)", root, size_bytes(data_dir) / 1e6)


def remove(data_dir: Path) -> None:
    kitten.remove(data_dir)
