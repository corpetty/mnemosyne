"""Pipeline stages, expressed as job runners.

Each stage takes a JobContext and the AppContext, reports progress through the
context, and updates session state through the session service. Routes only
submit these; they never run ML work inline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..jobs import JobContext
from ..models.session import Session, SessionStatus
from ..transcription.engine import AudioSource

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def sources_for_session(
    session: Session, per_source: bool, local_speaker_name: str
) -> list[AudioSource]:
    """Decide what to feed the engine.

    With separate mic and system recordings present, transcribe each: the mic
    file is the local user (labelled, not diarized) and the system file holds
    everyone else (diarized). A lone mic recording may contain a whole room,
    so it is diarized. Otherwise fall back to the mixed file.
    """
    recordings = [r for r in session.recordings if Path(r.path).is_file()]
    kinds = {r.source for r in recordings}
    if per_source and recordings and (len(recordings) > 1 or "system" in kinds):
        label_mic = "system" in kinds
        return [
            AudioSource(
                path=r.path,
                kind=r.source,
                speaker_label=local_speaker_name if (r.source == "mic" and label_mic) else None,
            )
            for r in recordings
        ]
    if session.audio_file:
        return [AudioSource(path=session.audio_file, kind="mixed")]
    return []


def transcribe_session(app: AppContext, session_id: str):
    """Build the transcription job runner for a session."""

    async def run(ctx: JobContext) -> dict:
        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        settings = app.settings
        sources = sources_for_session(
            session, settings.per_source_transcription, settings.local_speaker_name
        )
        if not sources:
            raise ValueError(f"Session {session_id} has no audio to transcribe")

        app.sessions.set_status(session_id, SessionStatus.TRANSCRIBING)
        try:
            ctx.update("Loading models...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Loading models..."})
            engine = await app.models.ensure_loaded()

            ctx.update("Transcribing...")
            ctx.emit({"type": "status", "session_id": session_id, "message": "Transcribing..."})
            logger.info(
                "Transcribing session %s from %d source(s): %s",
                session_id,
                len(sources),
                [(s.kind, s.speaker_label) for s in sources],
            )

            segments = []
            async for segment in engine.transcribe_sources(sources):
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
            return {"segments": len(segments), "sources": len(sources)}
        except Exception as e:
            app.sessions.set_status(session_id, SessionStatus.ERROR)
            ctx.emit({"type": "error", "session_id": session_id, "message": str(e)})
            raise

    return run
