"""Disk usage and audio retention. Only audio is ever removed; transcripts, summaries
and notes stay."""

from __future__ import annotations

import logging
import shutil
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
    has_transcript: bool


class StorageReport(ApiModel):
    data_dir: str
    recordings_bytes: int
    database_bytes: int
    sessions_with_audio: int
    retention_days: int
    largest: list[SessionUsage]


class CleanupResult(ApiModel):
    dry_run: bool
    sessions: list[SessionUsage]
    freed_bytes: int


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class StorageService:
    def __init__(self, repo: SessionRepository, data_dir: Path, recordings_dir: Path):
        self.repo = repo
        self.data_dir = data_dir
        self.recordings_dir = recordings_dir

    def _usage(self, s: SessionSummary) -> SessionUsage:
        return SessionUsage(
            session_id=s.id,
            name=s.name,
            created_at=s.created_at,
            audio_bytes=dir_size(self.recordings_dir / s.id),
            has_transcript=s.has_transcript,
        )

    def report(self, retention_days: int, top: int = 15) -> StorageReport:
        usages = [self._usage(s) for s in self.repo.list_summaries()]
        with_audio = [u for u in usages if u.audio_bytes > 0]
        db = sum(p.stat().st_size for p in self.data_dir.glob("mnemosyne.db*") if p.is_file())
        return StorageReport(
            data_dir=str(self.data_dir),
            recordings_bytes=dir_size(self.recordings_dir),
            database_bytes=db,
            sessions_with_audio=len(with_audio),
            retention_days=retention_days,
            largest=sorted(with_audio, key=lambda u: u.audio_bytes, reverse=True)[:top],
        )

    def delete_audio(self, session_id: str) -> int:
        """Remove a session's audio files and references. Returns bytes freed."""
        folder = self.recordings_dir / session_id
        freed = dir_size(folder)
        if not self.repo.clear_audio(session_id):
            raise KeyError(session_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        logger.info("Deleted audio for session %s (%d bytes)", session_id, freed)
        return freed

    def expired(self, retention_days: int, now: datetime | None = None) -> list[SessionUsage]:
        """Transcribed sessions older than the retention period that still hold audio."""
        if retention_days <= 0:
            return []
        cutoff = (now or datetime.now()) - timedelta(days=retention_days)
        return [
            u
            for u in (self._usage(s) for s in self.repo.list_summaries())
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
            for u in candidates:
                freed += self.delete_audio(u.session_id)
        else:
            freed = sum(u.audio_bytes for u in candidates)
        return CleanupResult(dry_run=dry_run, sessions=candidates, freed_bytes=freed)
