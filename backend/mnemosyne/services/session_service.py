"""Session lifecycle management on top of the repository."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..events import EventBus
from ..models.session import (
    DEFAULT_SESSION_NAME,
    Recording,
    Session,
    SessionStatus,
    SessionSummary,
    SummaryData,
)
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

    # The meeting types (settings.meeting_types), set by AppContext; a named meeting without a
    # type takes the first whose words its title contains (services/meeting_types.py).
    type_source = None

    def _typed(self, session: Session | None) -> Session | None:
        if session is None or session.meeting_type or self.type_source is None:
            return session
        from .meeting_types import match_type

        kind = match_type(session.name, self.type_source())
        if kind is None:
            return session
        fields: dict = {"meeting_type": kind.name}
        if kind.local_only:
            fields["local_only"] = True
        return self.repo.update_fields(session.id, **fields)

    def create_session(self, name: str = DEFAULT_SESSION_NAME) -> Session:
        session = self._typed(self.repo.save(Session(name=name)))
        logger.info("Created session %s: %s", session.id, session.name)
        return self._notify(session)

    def rename_session(self, session_id: str, name: str) -> Session | None:
        return self._notify(self._typed(self.repo.update_fields(session_id, name=name)))

    def set_meeting_type(self, session_id: str, name: str) -> Session | None:
        """Choose a meeting's type ("none" for none); a local-only type makes it local-only."""
        from .meeting_types import NONE

        fields: dict = {"meeting_type": name or NONE}
        kind = next(
            (t for t in (self.type_source() if self.type_source else []) if t.name == name), None
        )
        if kind is not None and kind.local_only:
            fields["local_only"] = True
        return self._notify(self.repo.update_fields(session_id, **fields))

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

    # Whether a meeting is being recorded right now (set by AppContext): its status stays
    # "recording" whatever a job finishing meanwhile (a transcription of earlier parts) sets.
    is_recording = None

    def _keeps_recording(self, session_id: str, status: SessionStatus) -> bool:
        return (
            self.is_recording is not None
            and status not in (SessionStatus.RECORDING, SessionStatus.ENCODING)
            and self.is_recording(session_id)
        )

    def set_status(self, session_id: str, status: SessionStatus) -> Session | None:
        if self._keeps_recording(session_id, status):
            return self.get_session(session_id)
        return self._notify(self.repo.update_fields(session_id, status=status))

    def set_audio(self, session_id: str, audio_file: str, recordings: list[Recording]) -> None:
        self.repo.add_recordings(session_id, recordings)
        self._notify(self.repo.update_fields(session_id, audio_file=audio_file))

    def set_transcript(self, session_id: str, segments: list[TranscriptSegment]) -> Session | None:
        speakers = list(dict.fromkeys(s.speaker for s in segments if s.speaker != "UNKNOWN"))
        self.repo.replace_segments(session_id, segments)
        fields: dict = {"participants": speakers}
        if not self._keeps_recording(session_id, SessionStatus.COMPLETED):
            fields["status"] = SessionStatus.COMPLETED
        return self._notify(self.repo.update_fields(session_id, **fields))

    def set_summary(
        self, session_id: str, summary: str, data: SummaryData | None = None
    ) -> Session | None:
        return self._notify(self.repo.update_fields(session_id, summary=summary, summary_data=data))
