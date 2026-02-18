"""Session management endpoints."""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...models.session import SessionStatus
from ...services.session_service import session_service

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionSummaryResponse(BaseModel):
    id: str
    name: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    has_transcript: bool
    has_summary: bool
    participant_count: int


class SessionDetailResponse(BaseModel):
    id: str
    name: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    audio_file: str | None
    transcript: list[dict]
    summary: str
    notes: str
    participants: list[str]


class CreateSessionRequest(BaseModel):
    name: str = "Untitled Session"


class RenameRequest(BaseModel):
    name: str


class NotesRequest(BaseModel):
    notes: str


@router.get("", response_model=list[SessionSummaryResponse])
async def list_sessions():
    """List all sessions."""
    sessions = session_service.list_sessions()
    return [
        SessionSummaryResponse(
            id=s.id,
            name=s.name,
            status=s.status,
            created_at=s.created_at,
            updated_at=s.updated_at,
            has_transcript=len(s.transcript) > 0,
            has_summary=len(s.summary) > 0,
            participant_count=len(s.participants),
        )
        for s in sessions
    ]


@router.post("", response_model=SessionDetailResponse)
async def create_session(request: CreateSessionRequest):
    """Create a new session."""
    session = session_service.create_session(request.name)
    return _session_detail(session)


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str):
    """Get session details including transcript."""
    session = session_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_detail(session)


@router.patch("/{session_id}", response_model=SessionDetailResponse)
async def rename_session(session_id: str, request: RenameRequest):
    """Rename a session."""
    session = session_service.rename_session(session_id, request.name)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_detail(session)


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """Delete a session."""
    if not session_service.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session deleted"}


@router.post("/{session_id}/notes", response_model=SessionDetailResponse)
async def update_notes(session_id: str, request: NotesRequest):
    """Update session notes."""
    session = session_service.update_notes(session_id, request.notes)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_detail(session)


def _session_detail(session) -> SessionDetailResponse:
    return SessionDetailResponse(
        id=session.id,
        name=session.name,
        status=session.status,
        created_at=session.created_at,
        updated_at=session.updated_at,
        audio_file=session.audio_file,
        transcript=[seg.model_dump() for seg in session.transcript],
        summary=session.summary,
        notes=session.notes,
        participants=session.participants,
    )
