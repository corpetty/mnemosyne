"""Session management endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.base import ApiModel
from ...models.session import DEFAULT_SESSION_NAME, Session, SessionSummary
from ...services.pipeline import transcribe_session
from ...services.stats import MeetingStats, meeting_stats
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateSessionRequest(ApiModel):
    name: str = DEFAULT_SESSION_NAME


class RenameRequest(ApiModel):
    name: str


class NotesRequest(ApiModel):
    notes: str


def _require(ctx: AppContext, session_id: str) -> Session:
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("", response_model=list[SessionSummary])
async def list_sessions(ctx: AppContext = Depends(get_ctx)):
    return ctx.sessions.list_sessions()


@router.post("", response_model=Session)
async def create_session(request: CreateSessionRequest, ctx: AppContext = Depends(get_ctx)):
    """Create a session; an untitled one made during a meeting is named after it."""
    from .audio import _apply_calendar

    return await _apply_calendar(ctx, ctx.sessions.create_session(request.name))


@router.get("/{session_id}", response_model=Session)
async def get_session(session_id: str, ctx: AppContext = Depends(get_ctx)):
    return _require(ctx, session_id)


@router.get("/{session_id}/stats", response_model=MeetingStats)
async def get_stats(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Talk time per speaker, turns and a who-spoke-when timeline."""
    return meeting_stats(_require(ctx, session_id).transcript)


@router.patch("/{session_id}", response_model=Session)
async def rename_session(
    session_id: str, request: RenameRequest, ctx: AppContext = Depends(get_ctx)
):
    session = ctx.sessions.rename_session(session_id, request.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/{session_id}")
async def delete_session(session_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.sessions.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted"}


@router.post("/{session_id}/notes", response_model=Session)
async def update_notes(session_id: str, request: NotesRequest, ctx: AppContext = Depends(get_ctx)):
    session = ctx.sessions.update_notes(session_id, request.notes)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/{session_id}/transcribe", response_model=Job)
async def transcribe(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Queue a transcription job for the session's recorded audio."""
    session = _require(ctx, session_id)
    if not session.audio_file:
        raise HTTPException(status_code=400, detail="Session has no audio to transcribe")
    if ctx.jobs.list(session_id=session_id, active_only=True):
        raise HTTPException(status_code=409, detail="Session already has an active job")
    return ctx.jobs.submit("transcribe", transcribe_session(ctx, session_id), session_id=session_id)
