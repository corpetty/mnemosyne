"""Turning encryption at rest on and off, unlocking, and sealing a meeting's audio.

What is encrypted: the database (sessions, transcripts, summaries, notes, search index, voice
profiles) with SQLCipher, and every audio file (recordings, mixes, clips) once a recording has
stopped. What is not: the WAV files pw-record writes while a recording runs, the Obsidian vault
(written for other apps to read), config.toml and the backend log.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import save_settings
from ..models.base import ApiModel
from ..models.session import Recording
from ..storage.crypto import (
    decrypt_file,
    derive,
    encrypt_file,
    is_encrypted,
    key_check,
    new_key,
    parse_recovery_code,
    recovery_code,
)
from ..storage.sqlite import convert_database

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


class EncryptionStatus(ApiModel):
    enabled: bool  # meetings on this computer are encrypted
    locked: bool  # ...and the key is not available: enter the recovery code


class EncryptionEnabled(ApiModel):
    recovery_code: str  # shown once: the only way back in without the keyring
    files: int  # audio files encrypted
    errors: list[str]


def status(app: AppContext) -> EncryptionStatus:
    return EncryptionStatus(enabled=app.settings.encrypt_at_rest, locked=app.locked)


def _session_files(app: AppContext, session_id: str) -> list[Path]:
    folder = app.settings.recordings_dir / session_id
    clips = folder / "clips"
    return sorted(clips.iterdir()) if clips.is_dir() else []


def _convert_audio(app: AppContext, session_id: str, key: bytes | None) -> tuple[int, list[str]]:
    """Encrypt (key) or decrypt (None) one meeting's recordings, mix and clips, and point the
    database at the new files. Returns (files converted, errors)."""
    session = app.sessions.get_session(session_id)
    if session is None:
        return 0, []
    converted, errors = 0, []
    # Originals are removed only once the database points at the new files: a crash between
    # the two leaves both, never a database pointing at nothing.
    originals: list[Path] = []

    def convert(path_str: str) -> str:
        nonlocal converted
        path = Path(path_str)
        if not path.is_file():
            return path_str
        try:
            if key is not None and not is_encrypted(path):
                converted += 1
                new = encrypt_file(path, key, remove=False)
                originals.append(path)
                return str(new)
            if key is None and is_encrypted(path):
                converted += 1
                new = decrypt_file(path, app.file_key, remove=False)
                originals.append(path)
                return str(new)
        except Exception as e:  # keep going; this file stays as it was
            logger.warning("Could not convert %s", path, exc_info=True)
            errors.append(f"{path.name}: {e}")
        return path_str

    audio = convert(session.audio_file) if session.audio_file else None
    recordings = [
        Recording(**{**r.model_dump(), "path": convert(r.path)}) for r in session.recordings
    ]
    if audio != session.audio_file or any(
        a.path != b.path for a, b in zip(recordings, session.recordings, strict=True)
    ):
        app.sessions.set_audio(session_id, audio or "", recordings)
    for original in originals:
        original.unlink(missing_ok=True)
    originals.clear()
    for clip in _session_files(app, session_id):  # clips are found by name, not stored
        if clip.name.endswith(".part"):
            continue
        convert(str(clip))
    for original in originals:
        original.unlink(missing_ok=True)
    return converted, errors


def seal_session_audio(app: AppContext, session_id: str) -> None:
    """After a recording stops or a file is imported: encrypt its audio when encryption is on."""
    key = app.file_key
    if app.settings.encrypt_at_rest and key is not None:
        _, errors = _convert_audio(app, session_id, key)
        for e in errors:
            logger.error("Audio left unencrypted: %s", e)


def _all_session_ids(app: AppContext) -> list[str]:
    return [s.id for s in app.sessions.list_sessions()]


def _replace_database(app: AppContext, db_key: bytes | None, current_key: bytes | None) -> None:
    """Rewrite the database encrypted with `db_key` (or plain, None) and reopen it in place.
    The new file is checked before it replaces the old one."""
    from ..storage.sqlite import SessionRepository

    db = app.settings.db_path
    tmp = db.with_name(db.name + ".converting")
    # Requests and the search indexer wait meanwhile instead of finding the database closed.
    with app.repo.exclusive():
        app.repo.close()
        try:
            convert_database(db, tmp, current_key, db_key)
            check = SessionRepository(tmp, db_key)  # opens, migrates, reads: or raises
            check.list_summaries()
            check.close()
        except Exception:
            tmp.unlink(missing_ok=True)
            app.repo.reopen(db, current_key)
            raise
        for extra in ("-wal", "-shm"):
            Path(str(db) + extra).unlink(missing_ok=True)
        os.replace(tmp, db)
        app.repo.reopen(db, db_key)


def enable(app: AppContext) -> EncryptionEnabled:
    if app.settings.encrypt_at_rest:
        raise ValueError("Meetings are already encrypted")
    master = new_key()
    app.keystore.set(master)  # raises when there is no keyring: nothing changed yet
    try:
        _replace_database(app, derive(master, "db"), None)
    except Exception:
        app.keystore.delete()
        raise
    app.master_key = master
    app.settings.encrypt_at_rest = True
    app.settings.encryption_check = key_check(master)
    save_settings(app.settings)
    files, errors = 0, []
    for sid in _all_session_ids(app):
        n, errs = _convert_audio(app, sid, app.file_key)
        files, errors = files + n, errors + errs
    from .assets import convert_files

    try:
        files += convert_files(app, app.file_key)
    except Exception as e:
        errors.append(f"resources: {e}")
    logger.info("Encryption on: database and %d files", files)
    return EncryptionEnabled(recovery_code=recovery_code(master), files=files, errors=errors)


def disable(app: AppContext) -> int:
    if not app.settings.encrypt_at_rest or app.master_key is None:
        raise ValueError("Meetings are not encrypted")
    files = 0
    for sid in _all_session_ids(app):
        n, errors = _convert_audio(app, sid, None)
        if errors:
            raise RuntimeError("Could not decrypt: " + "; ".join(errors))
        files += n
    from .assets import convert_files

    files += convert_files(app, None)
    _replace_database(app, None, derive(app.master_key, "db"))
    app.settings.encrypt_at_rest = False
    app.settings.encryption_check = ""
    save_settings(app.settings)
    app.keystore.delete()
    app.master_key = None
    logger.info("Encryption off: database and %d audio files decrypted", files)
    return files


def check_code(app: AppContext, code: str) -> bytes:
    """The master key from a recovery code, if it is this data directory's key."""
    master = parse_recovery_code(code)
    if app.settings.encryption_check and key_check(master) != app.settings.encryption_check:
        raise ValueError("That recovery code is not for these meetings")
    return master
