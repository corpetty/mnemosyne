"""Backups of meetings, audio and settings, and restoring one (services/backup.py)."""

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.base import ApiModel
from ...services.backup import (
    BackupInfo,
    RestoreResult,
    backup_dir,
    last_restore,
    list_backups,
    pending_restore,
    stage_restore,
)
from ..context import AppContext, backup_runner, get_ctx

router = APIRouter(prefix="/api/backup", tags=["backup"])


class BackupStatus(ApiModel):
    dir: str  # where backups are written and looked for
    backups: list[BackupInfo]  # newest first
    restore_pending: str | None = None  # a backup restored at the next start
    last_restore: RestoreResult | None = None


class RestoreRequest(ApiModel):
    name: str  # a backup in the backup folder (never a path)


class RestoreResponse(ApiModel):
    backup: BackupInfo
    restart_required: bool  # the backend restores on its next start


@router.get("", response_model=BackupStatus)
async def status(ctx: AppContext = Depends(get_ctx)):
    return BackupStatus(
        dir=str(backup_dir(ctx.settings)),
        backups=list_backups(ctx.settings),
        restore_pending=pending_restore(ctx.settings),
        last_restore=last_restore(ctx.settings),
    )


def _busy(ctx: AppContext) -> str | None:
    if ctx.active_recordings:
        return "Stop the recording first"
    kinds = {j.kind for j in ctx.jobs.list(active_only=True)} - {"live", "copilot"}
    return "Wait for the running jobs to finish" if kinds else None


@router.post("", response_model=Job)
async def back_up(ctx: AppContext = Depends(get_ctx)):
    """Write a backup now (a `backup` job; its result is the BackupInfo)."""
    if ctx.active_recordings:
        raise HTTPException(status_code=409, detail="Stop the recording first")
    if any(j.kind == "backup" for j in ctx.jobs.list(active_only=True)):
        raise HTTPException(status_code=409, detail="A backup is already being made")
    return ctx.jobs.submit("backup", backup_runner(ctx))


@router.post("/restore", response_model=RestoreResponse)
async def restore(request: RestoreRequest, ctx: AppContext = Depends(get_ctx)):
    """Restore a backup at the next start of the backend: the data there now is kept aside
    (pre-restore-<time>/ in the data folder), not deleted."""
    if (why := _busy(ctx)) is not None:
        raise HTTPException(status_code=409, detail=why)
    try:
        info = stage_restore(ctx.settings, request.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="No such backup") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return RestoreResponse(backup=info, restart_required=True)
