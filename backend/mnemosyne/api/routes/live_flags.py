"""Live lines marked "Not said" while recording: keyboard noise heard as "yeah". A final
transcription drops the lines they cover (services/live_flags.py)."""

from fastapi import APIRouter, Depends, HTTPException

from ...models.live_flag import LiveFlag, LiveFlagCreate
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/live-flags", tags=["live-flags"])


def _require_session(ctx: AppContext, session_id: str) -> None:
    if ctx.sessions.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("", response_model=list[LiveFlag])
async def list_flags(session_id: str, ctx: AppContext = Depends(get_ctx)):
    _require_session(ctx, session_id)
    return ctx.repo.list_live_flags(session_id)


@router.post("", response_model=LiveFlag)
async def add(session_id: str, request: LiveFlagCreate, ctx: AppContext = Depends(get_ctx)):
    _require_session(ctx, session_id)
    if not request.source.strip():
        raise HTTPException(status_code=422, detail="source is required")
    if request.end < request.start or request.start < 0:
        raise HTTPException(status_code=422, detail="The line's times are not valid")
    part = request.part
    if part is None:
        recording = ctx.active_recordings.get(session_id)
        part = recording.part if recording is not None and recording.is_recording else 0
    return ctx.repo.add_live_flag(
        session_id, request.source.strip(), request.start, request.end, request.text, part
    )


@router.delete("/{flag_id}")
async def delete(session_id: str, flag_id: str, ctx: AppContext = Depends(get_ctx)):
    _require_session(ctx, session_id)
    if not ctx.repo.delete_live_flag(session_id, flag_id):
        raise HTTPException(status_code=404, detail="Flag not found")
    return {"deleted": True}
