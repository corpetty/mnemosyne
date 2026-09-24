"""Disk usage, per-session audio deletion, and retention cleanup."""

from fastapi import APIRouter, Depends, HTTPException

from ...models.session import Session
from ...services.storage_service import CleanupResult, StorageReport
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["storage"])


@router.get("/storage", response_model=StorageReport)
async def storage_report(ctx: AppContext = Depends(get_ctx)):
    return ctx.storage.report(ctx.settings.audio_retention_days)


@router.delete("/sessions/{session_id}/audio", response_model=Session)
async def delete_session_audio(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Delete a session's audio files; transcript, summary and notes are kept."""
    if ctx.sessions.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session_id in ctx.busy_sessions():
        raise HTTPException(status_code=409, detail="Session is recording or has an active job")
    ctx.storage.delete_audio(session_id)
    ctx.bus.publish({"type": "session", "session_id": session_id, "status": "audio_deleted"})
    return ctx.sessions.get_session(session_id)


@router.post("/storage/cleanup", response_model=CleanupResult)
async def cleanup(
    dry_run: bool = True, days: int | None = None, ctx: AppContext = Depends(get_ctx)
):
    """Apply the retention rule now. Defaults to a dry run; `days` overrides the setting."""
    retention = ctx.settings.audio_retention_days if days is None else days
    if retention <= 0:
        raise HTTPException(status_code=400, detail="Retention is off (set a number of days)")
    result = ctx.storage.cleanup(retention, dry_run=dry_run, busy=ctx.busy_sessions())
    if not dry_run:
        for u in result.sessions:
            ctx.bus.publish(
                {"type": "session", "session_id": u.session_id, "status": "audio_deleted"}
            )
    return result
