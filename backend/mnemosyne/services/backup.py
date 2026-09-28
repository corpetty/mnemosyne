"""Back up what a lost or replaced machine would take with it, and restore it, here or elsewhere.

A backup is one uncompressed tar (the audio is compressed already):

    manifest.json           format, app version, time, number of meetings, encrypted or not
    settings.json           the settings without secrets (keys, tokens, feed URLs)
    mnemosyne.db            the meetings, speakers and voice profiles: a consistent copy, as
                            stored (encrypted when encryption at rest is on)
    recordings/<id>/...     audio (.ogg, or .enc) and clips
    assets/<id>/...         files attached to meetings as resources

With encryption on, a backup is as encrypted as the data, and the key is not in it: on a new
machine the recovery code unlocks it (the same locked screen as a missing key). Without
encryption anyone with the file can read the meetings.

Restoring is staged. `stage_restore` checks the backup and leaves restore-pending.json; the
next start (`apply_pending_restore`, before the database is opened) moves the current data
aside to pre-restore-<time>/ and unpacks the backup. Nothing is deleted, and a restore that
fails puts the data back.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import tarfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import SECRET_FIELDS, Settings, save_settings
from ..models.base import ApiModel

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

FORMAT = 1
PREFIX = "mnemosyne-backup-"
PENDING = "restore-pending.json"
RESTORED = "restore-result.json"
# The backup came from another recordings folder (another machine or user): its audio paths
# are rewritten once the database is open (after unlocking, when it is encrypted).
REBASE = "restore-rebase.json"
# Settings a restore leaves as they are: secrets (not in backups) and where things live here.
KEEP_ON_RESTORE = SECRET_FIELDS | {
    "data_dir",
    "backup_dir",
    "backup_interval_days",
    "backup_keep",
    "config_version",
}


class BackupInfo(ApiModel):
    name: str
    created_at: datetime
    size: int  # bytes
    sessions: int
    encrypted: bool
    app_version: str


class RestoreResult(ApiModel):
    name: str
    at: datetime
    kept_in: str  # where the data from before the restore is
    error: str | None = None


def default_dir() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "Mnemosyne backups"


def backup_dir(settings: Settings) -> Path:
    return Path(settings.backup_dir).expanduser() if settings.backup_dir else default_dir()


def _app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("mnemosyne-backend")
    except PackageNotFoundError:
        return "unknown"


def _files(recordings_dir: Path) -> list[Path]:
    """What a backup holds of the recordings folder: not WAVs (a capture still running)."""
    if not recordings_dir.is_dir():
        return []
    return sorted(p for p in recordings_dir.rglob("*") if p.is_file() and p.suffix != ".wav")


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def create_backup(app: AppContext, progress: Callable[[float], None] | None = None) -> BackupInfo:
    """Write a backup to the backup folder. Blocking: run it in a thread."""
    settings = app.settings
    dest = backup_dir(settings)
    dest.mkdir(parents=True, exist_ok=True)
    created = datetime.now().astimezone()
    name = f"{PREFIX}{created:%Y%m%d-%H%M%S}.tar"
    partial = dest / f".{name}.partial"
    snapshot = dest / f".{name}.db"
    files = _files(settings.recordings_dir)
    resources = _files(settings.assets_dir)
    total = sum(f.stat().st_size for f in files + resources) or 1
    manifest = {
        "format": FORMAT,
        "app_version": _app_version(),
        "created_at": created.isoformat(),
        "sessions": len(app.sessions.list_sessions()),
        "encrypted": bool(app.repo.encrypted),
        "recordings_dir": str(settings.recordings_dir),
    }
    public = {k: v for k, v in settings.public_dict().items() if k not in SECRET_FIELDS}
    try:
        app.repo.snapshot(snapshot)
        with tarfile.open(partial, "w") as tar:
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1).encode())
            _add_bytes(tar, "settings.json", json.dumps(public, indent=1).encode())
            tar.add(snapshot, arcname="mnemosyne.db")
            done = 0
            for folder, top, group in (
                (settings.recordings_dir, "recordings", files),
                (settings.assets_dir, "assets", resources),
            ):
                for f in group:
                    tar.add(f, arcname=f"{top}/{f.relative_to(folder).as_posix()}")
                    done += f.stat().st_size
                    if progress:
                        progress(done / total)
        partial.chmod(0o600)
        final = dest / name
        partial.rename(final)
    finally:
        snapshot.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
    logger.info("Backup written: %s (%d files)", final, len(files))
    return _info(final, manifest)


def _info(path: Path, manifest: dict) -> BackupInfo:
    return BackupInfo(
        name=path.name,
        created_at=manifest["created_at"],
        size=path.stat().st_size,
        sessions=manifest.get("sessions", 0),
        encrypted=manifest.get("encrypted", False),
        app_version=manifest.get("app_version", "unknown"),
    )


def read_manifest(path: Path) -> dict:
    """The manifest, the first member (so a large backup is not read through)."""
    with tarfile.open(path, "r:") as tar:
        first = tar.next()
        if first is None or first.name != "manifest.json":
            raise ValueError(f"{path.name} is not a Mnemosyne backup")
        manifest = json.loads(tar.extractfile(first).read())
    if manifest.get("format", 0) > FORMAT:
        raise ValueError(f"{path.name} is from a newer version of Mnemosyne")
    return manifest


def list_backups(settings: Settings) -> list[BackupInfo]:
    out = []
    folder = backup_dir(settings)
    for path in sorted(folder.glob(f"{PREFIX}*.tar"), reverse=True) if folder.is_dir() else []:
        try:
            out.append(_info(path, read_manifest(path)))
        except (OSError, ValueError, tarfile.TarError, KeyError) as e:
            logger.warning("Skipping %s: %s", path.name, e)
    return out


def prune(settings: Settings) -> list[str]:
    """Remove the oldest backups beyond `backup_keep` (only ours, by name)."""
    keep = max(1, settings.backup_keep)
    folder = backup_dir(settings)
    old = sorted(folder.glob(f"{PREFIX}*.tar"), reverse=True)[keep:] if folder.is_dir() else []
    for path in old:
        path.unlink(missing_ok=True)
        logger.info("Removed old backup %s", path.name)
    return [p.name for p in old]


def due(settings: Settings, now: float | None = None) -> bool:
    """Is an automatic backup due (backup_interval_days since the newest)?"""
    if settings.backup_interval_days <= 0:
        return False
    folder = backup_dir(settings)
    newest = max(
        (p.stat().st_mtime for p in folder.glob(f"{PREFIX}*.tar")) if folder.is_dir() else [],
        default=0.0,
    )
    return (now or time.time()) - newest >= settings.backup_interval_days * 86400


def stage_restore(settings: Settings, name: str) -> BackupInfo:
    """Check a backup in the backup folder and restore it at the next start."""
    if "/" in name or not (name.startswith(PREFIX) and name.endswith(".tar")):
        raise ValueError("Not a backup name")
    path = backup_dir(settings) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    info = _info(path, read_manifest(path))
    (settings.data_dir / PENDING).write_text(json.dumps({"path": str(path)}))
    logger.info("Restore of %s staged for the next start", name)
    return info


def pending_restore(settings: Settings) -> str | None:
    try:
        return Path(json.loads((settings.data_dir / PENDING).read_text())["path"]).name
    except (OSError, ValueError, KeyError):
        return None


def last_restore(settings: Settings) -> RestoreResult | None:
    try:
        return RestoreResult.model_validate_json((settings.data_dir / RESTORED).read_text())
    except (OSError, ValueError):
        return None


def _data_members(tar: tarfile.TarFile):
    for m in tar:
        if m.name == "mnemosyne.db" or m.name.startswith(("recordings/", "assets/")):
            yield m


def apply_pending_restore(settings: Settings) -> RestoreResult | None:
    """At startup, before the database is opened: carry out a staged restore."""
    pending = settings.data_dir / PENDING
    if not pending.exists():
        return None
    try:
        path = Path(json.loads(pending.read_text())["path"])
    finally:
        pending.unlink(missing_ok=True)  # never loop on a restore that fails
    data = settings.data_dir
    at = datetime.now().astimezone()
    aside = data / f"pre-restore-{at:%Y%m%d-%H%M%S}"
    aside.mkdir()
    moved = []
    names = ["mnemosyne.db", "mnemosyne.db-wal", "mnemosyne.db-shm", "recordings", "assets"]
    extracting = False
    try:
        manifest = read_manifest(path)
        for n in names:
            if (data / n).exists():
                (data / n).rename(aside / n)
                moved.append(n)
        extracting = True  # from here on, what is at data/<name> came from the backup
        with tarfile.open(path, "r:") as tar:
            tar.extractall(data, members=_data_members(tar), filter="data")
        with tarfile.open(path, "r:") as tar:
            saved = json.loads(tar.extractfile("settings.json").read())
        for key, value in saved.items():
            if key in Settings.model_fields and key not in KEEP_ON_RESTORE:
                setattr(settings, key, value)
        save_settings(settings)
        old_dir = manifest.get("recordings_dir")
        if old_dir and old_dir != str(settings.recordings_dir):
            (data / REBASE).write_text(
                json.dumps({"old": old_dir, "new": str(settings.recordings_dir)})
            )
        result = RestoreResult(name=path.name, at=at, kept_in=str(aside))
        logger.warning("Restored %s; the data from before is in %s", path.name, aside)
    except Exception as e:
        logger.exception("Restore of %s failed; putting the data back", path)
        if extracting:  # only ever remove what the restore wrote
            for n in names:
                target = data / n
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
        for n in moved:
            (aside / n).rename(data / n)
        if not any(aside.iterdir()):
            aside.rmdir()
        result = RestoreResult(name=path.name, at=at, kept_in="", error=str(e))
    (data / RESTORED).write_text(result.model_dump_json())
    return result


def apply_path_rebase(repo, settings: Settings) -> int:
    """After a restore from another recordings folder, once the database is open: point its
    audio paths at this one. Returns how many paths changed."""
    marker = settings.data_dir / REBASE
    try:
        paths = json.loads(marker.read_text())
    except (OSError, ValueError):
        return 0
    changed = repo.rebase_paths(paths["old"], paths["new"])
    marker.unlink(missing_ok=True)
    logger.info("Restored audio paths: %s -> %s (%d)", paths["old"], paths["new"], changed)
    return changed
