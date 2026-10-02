"""Files Mnemosyne downloads itself on first use: the ONNX diarization models and the built-in
summary model and its server (services/local_llm.py). Every download is pinned (a fixed URL) and
checked against its SHA-256 before it is used; a partial or wrong file never takes the place of a
good one.
"""

from __future__ import annotations

import hashlib
import logging
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]  # bytes done, bytes total (0 when unknown)


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str
    size: int  # bytes, for progress and "how much will this take"
    name: str  # the file's name in the target folder


class DownloadError(RuntimeError):
    pass


def present(folder: Path, item: Download) -> bool:
    return (folder / item.name).is_file()


def fetch(folder: Path, item: Download, progress: ProgressFn | None = None) -> Path:
    """The file, downloaded into `folder` if it is not there yet (blocking: run in a thread)."""
    dest = folder / item.name
    if dest.is_file():
        return dest
    folder.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    logger.info("Downloading %s (%d MB)", item.url, item.size // 1_000_000)
    try:
        with httpx.stream("GET", item.url, follow_redirects=True, timeout=60) as r:
            if r.status_code != 200:
                raise DownloadError(f"Could not download {item.name}: HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or item.size or 0)
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except httpx.HTTPError as e:
        part.unlink(missing_ok=True)
        raise DownloadError(f"Could not download {item.name}: {e}") from e
    if digest.hexdigest() != item.sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{item.name} did not match its checksum; not used")
    part.replace(dest)
    return dest


def extract(archive: Path, members: dict[str, str], folder: Path) -> None:
    """Extract chosen members of a .tar.* archive into `folder`, renamed ({member: new name}),
    never following paths out of it."""
    folder.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        for member, name in members.items():
            info = tar.getmember(member)
            src = tar.extractfile(info)
            if src is None:
                raise DownloadError(f"{member} is not a file in {archive.name}")
            out = folder / Path(name).name
            tmp = out.with_name(out.name + ".part")
            with tmp.open("wb") as f:
                while chunk := src.read(1 << 20):
                    f.write(chunk)
            tmp.chmod(info.mode & 0o755 or 0o644)
            tmp.replace(out)
