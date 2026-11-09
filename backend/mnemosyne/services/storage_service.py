"""Disk usage and audio retention. Only audio is ever removed; transcripts, summaries,
notes and the images in them stay (retention and "delete audio" leave notes_media/<id>)."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from ..models.base import ApiModel
from ..models.session import SessionSummary
from ..storage.sqlite import SessionRepository

logger = logging.getLogger(__name__)


class SessionUsage(ApiModel):
    session_id: str
    name: str
    created_at: datetime
    audio_bytes: int
    media_bytes: int  # the images in the meeting's notes (kept when its audio is deleted)
    has_transcript: bool


class StorageReport(ApiModel):
    data_dir: str
    recordings_bytes: int
    notes_media_bytes: int
    database_bytes: int
    sessions_with_audio: int
    retention_days: int
    largest: list[SessionUsage]


class CleanupResult(ApiModel):
    dry_run: bool
    sessions: list[SessionUsage]
    freed_bytes: int


class ErasureNotSealed(Exception):
    """The seal of an audio deletion could not be made, so nothing was deleted. The message is a
    plain reason for the person (services/records.py `seal_erasure`)."""


# Run before a meeting's audio goes: (session id, who, why, log) -> None, raising ErasureNotSealed.
# `log()` writes (once) the deletion's entry in the deletion log and returns its id.
EraseHook = Callable[[str, str, str, Callable[[], int]], None]


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class StorageService:
    def __init__(
        self,
        repo: SessionRepository,
        data_dir: Path,
        recordings_dir: Path,
        notes_media_dir: Path | None = None,
    ):
        self.repo = repo
        self.data_dir = data_dir
        self.recordings_dir = recordings_dir
        self.notes_media_dir = notes_media_dir or data_dir / "notes_media"
        # Set by AppContext.build: seals the erasure of the audio before it goes (records v2).
        self.before_erase: EraseHook | None = None

    def _usage(self, s: SessionSummary) -> SessionUsage:
        return SessionUsage(
            session_id=s.id,
            name=s.name,
            created_at=s.created_at,
            audio_bytes=dir_size(self.recordings_dir / s.id),
            media_bytes=dir_size(self.notes_media_dir / s.id),
            has_transcript=s.has_transcript,
        )

    def report(self, retention_days: int, top: int = 15) -> StorageReport:
        usages = [self._usage(s) for s in self.repo.list_summaries() if not s.origin]
        with_audio = [u for u in usages if u.audio_bytes > 0]
        db = sum(p.stat().st_size for p in self.data_dir.glob("mnemosyne.db*") if p.is_file())
        return StorageReport(
            data_dir=str(self.data_dir),
            recordings_bytes=dir_size(self.recordings_dir),
            notes_media_bytes=dir_size(self.notes_media_dir),
            database_bytes=db,
            sessions_with_audio=len(with_audio),
            retention_days=retention_days,
            largest=sorted(with_audio, key=lambda u: u.audio_bytes + u.media_bytes, reverse=True)[
                :top
            ],
        )

    def delete_audio(
        self,
        session_id: str,
        reason: str = "manual",
        *,
        by: str = "",
        role: str = "",
        detail: str = "",
        deletion: int | None = None,
    ) -> int:
        """Remove a session's audio files and references. Returns bytes freed. The images in its
        notes (notes_media/<id>) stay.

        Before anything goes, the erasure is sealed (`before_erase`: the files' plaintext digests
        stay in the record) and the deletion is entered in the deletion log (`by`, `role`, `detail`
        = why, as the person gave it; `deletion` when the caller already logged it). Raises
        ErasureNotSealed, with nothing deleted, when the seal cannot be made. `reason` is only
        what the meeting's history says ("manual", "retention")."""
        session = self.repo.get(session_id)
        if session is None:
            raise KeyError(session_id)
        logged: list[int] = [] if deletion is None else [deletion]

        def log() -> int:
            if not logged:
                logged.append(self.repo.log_deletion(session, "audio", by, role, detail))
            return logged[0]

        if self.before_erase is not None:
            self.before_erase(session_id, by, detail, log)
        log()
        folder = self.recordings_dir / session_id
        freed = dir_size(folder)
        if not self.repo.clear_audio(session_id):
            raise KeyError(session_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        try:  # the meeting's history says where its audio went (services/history.py)
            self.repo.add_event(
                session_id, "audio_deleted", None, {"bytes": freed, "reason": reason}
            )
        except Exception:
            logger.warning("Could not log the audio deletion of %s", session_id, exc_info=True)
        logger.info("Deleted audio for session %s (%d bytes)", session_id, freed)
        return freed

    def expired(self, retention_days: int, now: datetime | None = None) -> list[SessionUsage]:
        """Transcribed sessions older than the retention period that still hold audio."""
        if retention_days <= 0:
            return []
        cutoff = (now or datetime.now()) - timedelta(days=retention_days)
        return [
            u
            for u in (self._usage(s) for s in self.repo.list_summaries() if s.origin == "")
            if u.has_transcript and u.created_at < cutoff and u.audio_bytes > 0
        ]

    def cleanup(
        self,
        retention_days: int,
        dry_run: bool = False,
        busy: set[str] | None = None,
        now: datetime | None = None,
    ) -> CleanupResult:
        busy = busy or set()
        candidates = [u for u in self.expired(retention_days, now) if u.session_id not in busy]
        freed = 0
        if not dry_run:
            done = []
            for u in candidates:
                try:
                    freed += self.delete_audio(
                        u.session_id,
                        reason="retention",
                        by="retention",
                        role="system",
                        detail=f"older than {retention_days} days",
                    )
                except ErasureNotSealed as e:  # kept this pass; the next one tries again
                    logger.warning("Retention skipped session %s: %s", u.session_id, e)
                    continue
                done.append(u)
            candidates = done
        else:
            freed = sum(u.audio_bytes for u in candidates)
        return CleanupResult(dry_run=dry_run, sessions=candidates, freed_bytes=freed)
