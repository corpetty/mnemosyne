"""Session lifecycle management on top of the repository."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..events import EventBus
from ..models.session import Recording, Session, SessionStatus, SessionSummary, SummaryData
from ..models.transcript import TranscriptSegment
from ..storage.sqlite import SessionRepository

logger = logging.getLogger(__name__)


class SessionService:
    def __init__(self, repo: SessionRepository, recordings_dir: Path, bus: EventBus | None = None):
        self.repo = repo
        self.recordings_dir = recordings_dir
        self.bus = bus

    def _notify(self, session: Session | None) -> Session | None:
        if session is not None and self.bus is not None:
            self.bus.publish(
                {"type": "session", "session_id": session.id, "status": session.status.value}
            )
        return session

    # ---- CRUD ----------------------------------------------------------

    def list_sessions(self) -> list[SessionSummary]:
        return self.repo.list_summaries()

    def get_session(self, session_id: str) -> Session | None:
        return self.repo.get(session_id)

    def create_session(self, name: str = "Untitled Session") -> Session:
        session = self.repo.save(Session(name=name))
        logger.info("Created session %s: %s", session.id, session.name)
        return self._notify(session)

    def rename_session(self, session_id: str, name: str) -> Session | None:
        return self._notify(self.repo.update_fields(session_id, name=name))

    def update_notes(self, session_id: str, notes: str) -> Session | None:
        return self.repo.update_fields(session_id, notes=notes)

    def delete_session(self, session_id: str) -> bool:
        if not self.repo.delete(session_id):
            return False
        recording_dir = self.recordings_dir / session_id
        if recording_dir.exists():
            shutil.rmtree(recording_dir, ignore_errors=True)
        logger.info("Deleted session %s", session_id)
        if self.bus is not None:
            self.bus.publish({"type": "session", "session_id": session_id, "status": "deleted"})
        return True

    # ---- pipeline state ------------------------------------------------

    def set_status(self, session_id: str, status: SessionStatus) -> Session | None:
        return self._notify(self.repo.update_fields(session_id, status=status))

    def set_audio(self, session_id: str, audio_file: str, recordings: list[Recording]) -> None:
        self.repo.add_recordings(session_id, recordings)
        self._notify(self.repo.update_fields(session_id, audio_file=audio_file))

    def set_transcript(self, session_id: str, segments: list[TranscriptSegment]) -> Session | None:
        speakers = list(dict.fromkeys(s.speaker for s in segments if s.speaker != "UNKNOWN"))
        self.repo.replace_segments(session_id, segments)
        return self._notify(
            self.repo.update_fields(
                session_id, participants=speakers, status=SessionStatus.COMPLETED
            )
        )

    def set_summary(
        self, session_id: str, summary: str, data: SummaryData | None = None
    ) -> Session | None:
        return self._notify(self.repo.update_fields(session_id, summary=summary, summary_data=data))
