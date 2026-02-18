"""Session lifecycle management."""

import logging
from pathlib import Path

from ..config import DATA_DIR
from ..models.session import Session, SessionStatus

logger = logging.getLogger(__name__)

SESSIONS_DIR = DATA_DIR / "sessions"


class SessionService:
    """CRUD operations and lifecycle management for sessions."""

    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[Session]:
        """List all sessions, sorted by creation date descending."""
        sessions = []
        for path in SESSIONS_DIR.glob("*.json"):
            try:
                sessions.append(Session.load(path))
            except Exception:
                logger.warning("Failed to load session: %s", path)
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return sessions

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        return Session.load(path)

    def create_session(self, name: str = "Untitled Session") -> Session:
        """Create a new session and persist it."""
        session = Session(name=name)
        session.save(DATA_DIR)
        logger.info("Created session %s: %s", session.id, session.name)
        return session

    def update_session(self, session: Session) -> Session:
        """Save updated session to disk."""
        session.save(DATA_DIR)
        return session

    def rename_session(self, session_id: str, name: str) -> Session | None:
        """Rename an existing session."""
        session = self.get_session(session_id)
        if session is None:
            return None
        session.name = name
        session.save(DATA_DIR)
        return session

    def update_notes(self, session_id: str, notes: str) -> Session | None:
        """Update session notes."""
        session = self.get_session(session_id)
        if session is None:
            return None
        session.notes = notes
        session.save(DATA_DIR)
        return session

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its data."""
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return False

        # Also clean up recordings
        recording_dir = DATA_DIR / "recordings" / session_id
        if recording_dir.exists():
            import shutil
            shutil.rmtree(recording_dir)

        path.unlink()
        logger.info("Deleted session %s", session_id)
        return True

    def set_status(self, session_id: str, status: SessionStatus) -> Session | None:
        """Update session status."""
        session = self.get_session(session_id)
        if session is None:
            return None
        session.status = status
        session.save(DATA_DIR)
        return session

    def set_audio_file(self, session_id: str, audio_file: str) -> Session | None:
        """Set the audio file path for a session."""
        session = self.get_session(session_id)
        if session is None:
            return None
        session.audio_file = audio_file
        session.save(DATA_DIR)
        return session

    def set_transcript(
        self, session_id: str, segments: list[dict]
    ) -> Session | None:
        """Set transcript segments for a session."""
        from ..models.transcript import TranscriptSegment

        session = self.get_session(session_id)
        if session is None:
            return None
        session.transcript = [TranscriptSegment.model_validate(s) for s in segments]

        # Extract unique speakers as participants
        speakers = list(dict.fromkeys(
            seg.speaker for seg in session.transcript if seg.speaker != "UNKNOWN"
        ))
        session.participants = speakers
        session.status = SessionStatus.COMPLETED
        session.save(DATA_DIR)
        return session

    def set_summary(self, session_id: str, summary: str) -> Session | None:
        """Set summary for a session."""
        session = self.get_session(session_id)
        if session is None:
            return None
        session.summary = summary
        session.save(DATA_DIR)
        return session


# Global singleton
session_service = SessionService()
