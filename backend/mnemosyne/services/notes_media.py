"""Images in the notes (docs/plans/2026-11-04-images-tasks.md, task 4).

An image line of the notes document carries the closed `image` mark on its `\\n`, valued
`<sha256 hex>.<webp|jpeg>@<w>x<h>` (`IMAGE_VALUE`, the same regex as notes-core and
src/lib/notes/images.ts); the sha is of the stored bytes. The bytes never enter the document,
its log or library sync's `notes_doc`: they are blobs of the meeting, one file each under
<data_dir>/notes_media/<session_id>/<sha>.<ext> (sealed as `.enc` when encryption at rest is on,
as services/assets.py `store_file` does), with a `notes_media` row (who uploaded it, its size and
dimensions, `last_seen_at`).

What the document references is learnt only from the sidecar: every `notes doc` answer carries
`media` (the distinct values of its image lines, in document order) and each fold-result site
calls `seen`, which touches the rows it names and tells `MISSING_HOOKS` about values whose bytes
are not here. `sweep` (at startup and daily, only with a sidecar) deletes blobs no document has
referenced for `UNSEEN_DAYS`, uploads never used included; before it deletes, it folds the
meeting's document once more, so a meeting nobody opened keeps its images.

Caps are decimal, as Corey gave them: `IMAGE_MAX_BYTES` a blob, `MEETING_MEDIA_MAX_BYTES` the
sum of a meeting's blobs (the new one included). A new blob is announced on the bus as
`{"type": "notes_media", "session_id", "sha256"}` and to `ON_STORED`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

IMAGE_VALUE = re.compile(r"^([0-9a-f]{64})\.(webp|jpeg)@([1-9][0-9]{0,4})x([1-9][0-9]{0,4})$")
SHA = re.compile(r"^[0-9a-f]{64}$")
IMAGE_MAX_BYTES = 2_000_000  # a blob, as stored
MEETING_MEDIA_MAX_BYTES = 50_000_000  # a meeting's blobs together
UNSEEN_DAYS = 7  # unreferenced for this long: swept
SWEEP_EVERY = 24 * 3600.0
MEDIA_TYPES = {"webp": "image/webp", "jpeg": "image/jpeg"}
# An image line in markdown, as notes-core renders it (the caption is escaped link text).
_IMAGE_LINE = re.compile(r"^!\[(?:[^\]\\\n]|\\.)*\]\(mnemosyne:image/([^)\s]+)\)$", re.MULTILINE)


@dataclass(frozen=True)
class MediaInfo:
    sha256: str
    ext: str
    width: int
    height: int
    size: int
    created: bool  # stored now (False: it was here already)


# Hooks of the services that move blobs between machines (guest bridge, library sync, shares).
# Each is called with (app, session_id, values) from a fold-result site: in that thread, with
# the meeting's fold lock held and on every fold that still lacks them, so it must only note or
# schedule the fetch (and dedup), never do it there. A hook that raises is logged.
MISSING_HOOKS: list[Callable[[AppContext, str, list[str]], None]] = []
# Called with (app, session_id, MediaInfo) after a NEW blob is stored, from store's thread.
ON_STORED: list[Callable[[AppContext, str, MediaInfo], None]] = []


class MediaError(ValueError):
    """An upload refused; `status` is the HTTP answer."""

    status = 400


class MediaTypeError(MediaError):
    status = 415


class MediaTooLarge(MediaError):
    status = 413


class MediaGone(MediaError):
    status = 404


def parse_value(value: str) -> tuple[str, str, int, int] | None:
    """(sha, ext, width, height) of an image mark's value; None for anything else."""
    m = IMAGE_VALUE.match(value) if isinstance(value, str) else None
    return (m[1], m[2], int(m[3]), int(m[4])) if m else None


def values_in(markdown: str) -> list[str]:
    """The valid image values of a markdown text's image lines, distinct, in order."""
    found = (m[1] for m in _IMAGE_LINE.finditer(markdown or ""))
    return list(dict.fromkeys(v for v in found if IMAGE_VALUE.match(v)))


def sniff(data: bytes) -> str | None:
    """The type by the magic bytes: "webp", "jpeg", or None for anything else."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


def dimensions(data: bytes, ext: str) -> tuple[int, int] | None:
    """(width, height) from the image's header; None when it cannot be read."""
    try:
        if ext == "webp":
            chunk = data[12:16]
            if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack_from("<HH", data, 26)
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L" and data[20] == 0x2F:
                bits = struct.unpack_from("<I", data, 21)[0]
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            return None
        if ext == "jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if marker == 0xFF:  # fill byte
                    i += 1
                    continue
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack_from(">HH", data, i + 5)
                    return w, h
                if marker == 0x01 or 0xD0 <= marker <= 0xD9:
                    i += 2
                    continue
                i += 2 + struct.unpack_from(">H", data, i + 2)[0]
    except (IndexError, struct.error):
        return None
    return None


# ---- files ---------------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock(session_id: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(session_id, threading.Lock())


def _folder(app: AppContext, session_id: str) -> Path:
    base = app.settings.notes_media_dir
    folder = base / session_id
    if (
        not session_id
        or session_id in (".", "..")
        or "/" in session_id
        or os.sep in session_id
        or not folder.resolve().is_relative_to(base.resolve())
    ):
        raise ValueError("Not a meeting id")
    return folder


def _now() -> str:
    return datetime.now().isoformat()


def path(app: AppContext, session_id: str, sha: str) -> Path | None:
    """The blob's file (plain or `.enc`), or None when its bytes are not here."""
    if not SHA.match(sha or ""):
        return None
    folder = _folder(app, session_id)
    for ext in MEDIA_TYPES:
        for name in (f"{sha}.{ext}.enc", f"{sha}.{ext}"):
            if (folder / name).is_file():
                return folder / name
    return None


def ext_of(file: Path) -> str:
    """The type of a blob's file name: "webp" or "jpeg"."""
    name = file.name.removesuffix(".enc")
    return name.rsplit(".", 1)[-1]


def read(app: AppContext, session_id: str, sha: str) -> bytes:
    """The blob's bytes, decrypted. FileNotFoundError when they are not here; PermissionError
    when they are sealed and the key is missing."""
    from ..storage.crypto import plaintext

    file = path(app, session_id, sha)
    if file is None:
        raise FileNotFoundError(f"No image {sha} in {session_id}")
    with plaintext(file, app.file_key) as plain:
        return plain.read_bytes()


def store(app: AppContext, session_id: str, sha: str, data: bytes, *, by: str = "") -> MediaInfo:
    """Keep `data` as the meeting's blob `sha`: the hash, the type and the caps checked, written
    through a `.part`, sealed when encryption is on, its row written (a new version of the
    meeting). Storing what is here already changes nothing (`created` False). Access is the
    caller's to check. Raises MediaError (with its HTTP status)."""
    from ..storage.crypto import encrypt_file

    if not SHA.match(sha or ""):
        raise MediaError("Not an image hash (64 lowercase hex characters)")
    if len(data) > IMAGE_MAX_BYTES:
        raise MediaTooLarge("An image may be at most 2 MB")
    if hashlib.sha256(data).hexdigest() != sha:
        raise MediaError("The image does not match its hash")
    ext = sniff(data)
    if ext is None:
        raise MediaTypeError("Only WebP and JPEG images are kept")
    width, height = dimensions(data, ext) or (0, 0)
    folder = _folder(app, session_id)
    with _lock(session_id):
        rows = {r["sha256"]: r for r in app.repo.media_rows(session_id)}
        row = rows.get(sha)
        if row is not None and path(app, session_id, sha) is not None:
            app.repo.touch_media(session_id, [sha], _now())
            return MediaInfo(sha, row["ext"], row["width"], row["height"], row["size"], False)
        others = sum(r["size"] for s, r in rows.items() if s != sha)
        if others + len(data) > MEETING_MEDIA_MAX_BYTES:
            raise MediaTooLarge("This meeting's images are at their limit of 50 MB")
        if path(app, session_id, sha) is None:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{sha}.{ext}"
            part = dest.with_name(dest.name + ".part")
            with part.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            part.replace(dest)
            if app.settings.encrypt_at_rest and app.file_key is not None:
                encrypt_file(dest, app.file_key)
        if not app.repo.put_media_row(session_id, sha, ext, width, height, len(data), by):
            remove_blob(app, session_id, sha)
            raise MediaGone("Session not found")
    info = MediaInfo(sha, ext, width, height, len(data), True)
    _announce(app, session_id, info)
    return info


def _announce(app: AppContext, session_id: str, info: MediaInfo) -> None:
    from . import shared_notes

    event = {"type": "notes_media", "session_id": session_id, "sha256": info.sha256}
    try:
        shared_notes._on_loop(app, app.bus.publish, event)
    except Exception:
        logger.warning("Could not announce image %s of %s", info.sha256, session_id)
    for hook in list(ON_STORED):
        try:
            hook(app, session_id, info)
        except Exception:
            logger.warning("A notes media hook failed for %s", session_id, exc_info=True)


def remove_blob(app: AppContext, session_id: str, sha: str) -> None:
    """Delete a blob's file(s), whatever its type and sealing (not its row)."""
    folder = _folder(app, session_id)
    for ext in MEDIA_TYPES:
        for name in (f"{sha}.{ext}", f"{sha}.{ext}.enc", f"{sha}.{ext}.part"):
            (folder / name).unlink(missing_ok=True)


def remove_session(app: AppContext, session_id: str) -> None:
    """The meeting's folder goes (its rows go with the meeting: ON DELETE CASCADE)."""
    shutil.rmtree(_folder(app, session_id), ignore_errors=True)


def move_session(app: AppContext, src: str, dst: str) -> int:
    """Combining meetings: `src`'s blobs become `dst`'s (files moved, rows re-keyed; one `dst`
    has already stays as it is). Returns the blobs moved."""
    if src == dst:
        return 0
    moved = 0
    target = _folder(app, dst)
    first, second = sorted((src, dst))  # one order for every caller
    with _lock(first), _lock(second):
        have = {r["sha256"] for r in app.repo.media_rows(dst)}
        for row in app.repo.media_rows(src):
            sha = row["sha256"]
            file = path(app, src, sha)
            if sha in have or file is None:
                continue
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file), target / file.name)
            app.repo.put_media_row(
                dst, sha, row["ext"], row["width"], row["height"], row["size"], row["by"]
            )
            moved += 1
        app.repo.delete_media_rows(src, [r["sha256"] for r in app.repo.media_rows(src)])
    remove_session(app, src)
    return moved


def total_bytes(app: AppContext, session_id: str) -> int:
    """What the meeting's blobs take on disk (sealed size when sealed)."""
    folder = _folder(app, session_id)
    if not folder.is_dir():
        return 0
    return sum(f.stat().st_size for f in folder.iterdir() if f.is_file())


# ---- what the document references -----------------------------------------------------------


def seen(app: AppContext, session_id: str, media) -> None:
    """The fold hook: `media` is a `notes doc` answer's list (None from a sidecar that does not
    report it: nothing happens). Touches the rows it names; values whose bytes are not here go
    to `MISSING_HOOKS`. Never raises."""
    if not isinstance(media, list):
        return
    try:
        values = list(dict.fromkeys(v for v in media if parse_value(v) is not None))
        if not values:
            return
        app.repo.touch_media(session_id, [v[:64] for v in values], _now())
        absent = [v for v in values if path(app, session_id, v[:64]) is None]
    except Exception:
        logger.warning("Could not note the images of %s", session_id, exc_info=True)
        return
    if absent:
        _missing_hooks(app, session_id, absent)


def _missing_hooks(app: AppContext, session_id: str, values: list[str]) -> None:
    for hook in list(MISSING_HOOKS):
        try:
            hook(app, session_id, values)
        except Exception:
            logger.warning("A missing-media hook failed for %s", session_id, exc_info=True)


def _value(row: dict) -> str | None:
    value = f"{row['sha256']}.{row['ext']}@{row['width']}x{row['height']}"
    return value if IMAGE_VALUE.match(value) else None


def missing(app: AppContext, session_id: str) -> list[str]:
    """Values the meeting has a row for, or its notes show, whose bytes are not here (a row
    whose dimensions are unknown cannot be named and is left out)."""
    source = app.repo.notes_source(session_id)
    values = values_in(source[0]) if source else []
    values += [v for r in app.repo.media_rows(session_id) if (v := _value(r))]
    first: dict[str, str] = {}  # sha -> its first value
    for v in values:
        first.setdefault(v[:64], v)
    return [v for sha, v in first.items() if path(app, session_id, sha) is None]


def _referenced_locked(app: AppContext, session_id: str, binary: str) -> set[str]:
    """The shas the meeting's document (its current state, folded but not stored) and its
    `Session.notes` reference. Raises NotesDocError, also for a sidecar that does not report
    `media` (no knowledge: nothing may be deleted)."""
    from . import shared_notes

    shas: set[str] = set()
    snapshot, ops, _ = shared_notes._state(app, session_id)
    if snapshot is not None or ops:
        reply = shared_notes._run_doc(
            binary,
            {
                "op": "fold",
                "notes": shared_notes.notes_origin(session_id),
                "snapshot": snapshot,
                "frames": [shared_notes._b64(f) for _, f in ops],
            },
        )
        media = reply.get("media")
        if not isinstance(media, list):
            raise shared_notes.NotesDocError("the sidecar does not report media")
        shas |= {p[0] for v in media if (p := parse_value(v))}
    source = app.repo.notes_source(session_id)
    if source:
        shas |= {v[:64] for v in values_in(source[0])}
    return shas


def sweep(app: AppContext, now: datetime | None = None) -> int:
    """Delete the blobs (files and rows) no document has referenced for `UNSEEN_DAYS`. Only
    with a sidecar that folds (else nothing is known of the references); each candidate
    meeting's document is folded first, under its fold lock, and what it still shows is kept
    and touched. Blocking (subprocesses): call it in a thread. Returns the blobs deleted."""
    from . import shared_notes

    if not shared_notes.available():
        return 0
    binary = shared_notes._binary()
    if binary is None:
        return 0
    now = now or datetime.now()
    by_session: dict[str, list[dict]] = {}
    for row in app.repo.stale_media((now - timedelta(days=UNSEEN_DAYS)).isoformat()):
        by_session.setdefault(row["session_id"], []).append(row)
    removed = 0
    for session_id, rows in by_session.items():
        try:
            with shared_notes.hub(app).lock(session_id), _lock(session_id):
                still = _referenced_locked(app, session_id, binary)
                app.repo.touch_media(
                    session_id, [r["sha256"] for r in rows if r["sha256"] in still], now.isoformat()
                )
                gone = [r["sha256"] for r in rows if r["sha256"] not in still]
                for sha in gone:
                    remove_blob(app, session_id, sha)
                removed += app.repo.delete_media_rows(session_id, gone)
        except shared_notes.NotesDocError as e:
            logger.warning("Images of %s not swept: %s", session_id, e)
        except Exception:
            logger.warning("Images of %s not swept", session_id, exc_info=True)
    if removed:
        logger.info("Removed %d unused image(s) from the notes", removed)
    return removed
