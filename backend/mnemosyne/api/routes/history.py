"""A meeting's history: its parts, the files behind them, audio waiting to be saved into it,
and what happened to it (services/history.py)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.history import SessionHistory
from ...services import history
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}", tags=["history"])


@router.get("/history", response_model=SessionHistory)
async def get_history(session_id: str, ctx: AppContext = Depends(get_ctx)):
    found = await asyncio.to_thread(history.build, ctx, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return found


@router.post("/recover", response_model=Job)
async def recover(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Save audio that was recorded but is not in the meeting yet (an interrupted recording,
    or one whose save failed) as its next part."""
    from ...services.recovery import pending_recordings, recover_session

    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session_id in ctx.busy_sessions():
        raise HTTPException(status_code=409, detail="The meeting is recording or busy; try later")
    folder = ctx.settings.recordings_dir / session_id
    if not folder.is_dir() or not await asyncio.to_thread(pending_recordings, folder, session):
        raise HTTPException(status_code=404, detail="No audio is waiting to be saved")
    return ctx.jobs.submit("recover", recover_session(ctx, session_id), session_id)
