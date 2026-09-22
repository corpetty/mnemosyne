"""Known-voice profiles and per-session speaker renaming."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...models.session import Session
from ...models.speaker import SpeakerProfileSummary
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["speakers"])


class RenameSpeakerRequest(BaseModel):
    label: str
    name: str
    enroll: bool = True


class RenameProfileRequest(BaseModel):
    name: str


def _summary(p) -> SpeakerProfileSummary:
    return SpeakerProfileSummary(
        id=p.id,
        name=p.name,
        sample_count=p.sample_count,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.post("/sessions/{session_id}/speakers/rename", response_model=Session)
async def rename_session_speaker(
    session_id: str, request: RenameSpeakerRequest, ctx: AppContext = Depends(get_ctx)
):
    """Rename a speaker label in one session and (by default) remember the voice."""
    try:
        session = ctx.speakers.rename(session_id, request.label, request.name, request.enroll)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    ctx.bus.publish({"type": "session", "session_id": session_id, "status": session.status.value})
    return session


@router.get("/sessions/{session_id}/speakers")
async def session_speakers(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Which labels in this session have a stored voice embedding (can be enrolled)."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    with_voice = set(ctx.repo.get_session_embeddings(session_id))
    return [{"label": p, "has_voice": p in with_voice} for p in session.participants]


@router.get("/speakers", response_model=list[SpeakerProfileSummary])
async def list_speakers(ctx: AppContext = Depends(get_ctx)):
    return [_summary(p) for p in ctx.repo.list_speakers()]


@router.patch("/speakers/{speaker_id}", response_model=SpeakerProfileSummary)
async def rename_profile(
    speaker_id: str, request: RenameProfileRequest, ctx: AppContext = Depends(get_ctx)
):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name must not be empty")
    if (other := ctx.repo.get_speaker_by_name(name)) and other.id != speaker_id:
        raise HTTPException(status_code=409, detail="A speaker with that name already exists")
    profile = ctx.repo.rename_speaker(speaker_id, name)
    if profile is None:
        raise HTTPException(status_code=404, detail="Speaker not found")
    return _summary(profile)


@router.delete("/speakers/{speaker_id}")
async def delete_profile(speaker_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.repo.delete_speaker(speaker_id):
        raise HTTPException(status_code=404, detail="Speaker not found")
    return {"message": "Speaker deleted"}
