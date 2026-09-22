"""Pipeline stages, expressed as job runners.

Each stage takes a JobContext and the AppContext, reports progress through the
context, and updates session state through the session service. Routes only
submit these; they never run ML work inline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..jobs import JobContext
from ..models.session import SessionStatus

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def transcribe_session(app: AppContext, session_id: str):
    """Build the transcription job runner for a session."""

    async def run(ctx: JobContext) -> dict:
        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        if not session.audio_file:
            raise ValueError(f"Session {session_id} has no audio to transcribe")

        app.sessions.set_status(session_id, SessionStatus.TRANSCRIBING)
        try:
            ctx.update("Loading models...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Loading models..."})
            engine = await app.models.ensure_loaded()

            ctx.update("Transcribing...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Transcribing..."})

            segments = []
            async for segment in engine.transcribe(session.audio_file):
                segments.append(segment)
                ctx.emit(
                    {
                        "type": "transcription",
                        "session_id": session_id,
                        "segment": segment.model_dump(),
                    }
                )

            app.sessions.set_transcript(session_id, segments)
            ctx.update("Transcription complete")
            ctx.emit(
                {"type": "status", "session_id": session_id, "message": "Transcription complete"}
            )
            return {"segments": len(segments)}
        except Exception as e:
            app.sessions.set_status(session_id, SessionStatus.ERROR)
            ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
            raise

    return run
