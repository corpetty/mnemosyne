"""Meeting records (services/records.py): versions, seals and their verification, legal holds,
deletions, and exports for an exam."""

import asyncio
import json
import time
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ... import access
from ...jobs import Job
from ...models.base import ApiModel
from ...services import history, records
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["records"])

# An export holds the audio unencrypted: downloaded once, then gone; gone after a day anyway.
EXPORT_KEEP_SECONDS = 24 * 3600


class VersionInfo(ApiModel):
    id: int
    at: datetime
    reason: str  # what replaced this version
    by: str


class MeetingRecord(ApiModel):
    kept_until: date | None
    legal_hold: str  # the hold's reason, "" when none
    versions: list[VersionInfo]
    verification: records.Verification


class Version(VersionInfo):
    transcript: list[dict]
    summary: str


class LegalHoldRequest(ApiModel):
    reason: str  # "" lifts the hold


class Deletion(ApiModel):
    at: datetime
    session_id: str
    name: str
    created_at: datetime
    what: str  # "meeting" or "audio"
    by: str
    role: str
    reason: str


def _session(ctx: AppContext, session_id: str):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _compliance() -> None:
    """Reviewers (compliance) and admins."""
    p = access.principal()
    if p is not None and p.role not in (access.REVIEWER, access.ADMIN):
        raise access.Forbidden("Only a reviewer or an admin can do this")


@router.get("/sessions/{session_id}/records", response_model=MeetingRecord)
async def meeting_record(session_id: str, ctx: AppContext = Depends(get_ctx)):
    session = _session(ctx, session_id)
    check = await asyncio.to_thread(records.verify, ctx, session_id)  # may hash audio once
    return MeetingRecord(
        kept_until=records.kept_until(ctx, session),
        legal_hold=session.legal_hold,
        versions=[VersionInfo(**v) for v in ctx.repo.versions(session_id)],
        verification=check,
    )


@router.get("/sessions/{session_id}/versions/{version_id}", response_model=Version)
async def version(session_id: str, version_id: int, ctx: AppContext = Depends(get_ctx)):
    _session(ctx, session_id)
    for v in ctx.repo.versions(session_id, with_content=True):
        if v["id"] == version_id:
            content = v.pop("content")
            return Version(**v, transcript=content["transcript"], summary=content["summary"])
    raise HTTPException(status_code=404, detail="Version not found")


@router.put("/sessions/{session_id}/legal-hold", response_model=MeetingRecord)
async def legal_hold(
    session_id: str, request: LegalHoldRequest, ctx: AppContext = Depends(get_ctx)
):
    """Put a meeting on legal hold (nothing of it can be deleted) or lift it."""
    _compliance()
    session = _session(ctx, session_id)
    reason = request.reason.strip()[:200]
    ctx.repo.set_legal_hold(session_id, reason)
    who = access.principal()
    by = who.name if who else ""
    if reason:
        history.log(ctx, session_id, "legal_hold", reason=reason, by=by)
    elif session.legal_hold:
        history.log(ctx, session_id, "legal_hold_lifted", reason=session.legal_hold, by=by)
    return await meeting_record(session_id, ctx)


@router.get("/records/deletions", response_model=list[Deletion])
async def deletions(
    start: date | None = None, end: date | None = None, ctx: AppContext = Depends(get_ctx)
):
    _compliance()
    return ctx.repo.deletions(start.isoformat() if start else "", end.isoformat() if end else "")


@router.post("/records/export", response_model=Job)
async def export(request: records.ExportRequest, ctx: AppContext = Depends(get_ctx)):
    """Meetings (these ids, or created start..end) as a zip for an exam: a `records_export`
    job whose result names the file to download from /api/records/exports/{id}."""
    if not request.session_ids and request.start is None:
        raise HTTPException(status_code=400, detail="Choose meetings or a start date")
    for sid in request.session_ids:
        _session(ctx, sid)
        history.log_access(ctx, sid, "exported")
    folder = records.exports_dir(ctx)
    _prune(folder)
    owner = access.user_id()

    async def run(job) -> dict:
        job.update("Collecting the meetings", progress=0.05)
        dest = folder / f"{job.job.id}.zip"
        done = await asyncio.to_thread(
            records.export_zip,
            ctx,
            request,
            dest,
            lambda f: job.update("Writing the export", progress=0.05 + 0.9 * f),
        )
        dest.with_suffix(".json").write_text(json.dumps({"owner_id": owner}))
        return {"export_id": job.job.id, **done}

    return ctx.jobs.submit("records_export", run)


@router.get("/records/exports/{export_id}")
async def download(export_id: str, ctx: AppContext = Depends(get_ctx)):
    folder = records.exports_dir(ctx)
    path = folder / f"{export_id}.zip"
    meta = folder / f"{export_id}.json"
    if not export_id.isalnum() or not path.is_file() or not meta.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    owner = json.loads(meta.read_text()).get("owner_id", "")
    if owner != access.user_id() and not access.is_admin():
        raise HTTPException(status_code=404, detail="Export not found")
    name = f"mnemosyne-records-{datetime.now():%Y%m%d}-{export_id}.zip"

    def gone() -> None:
        path.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)

    return FileResponse(
        path, media_type="application/zip", filename=name, background=BackgroundTask(gone)
    )


def _prune(folder) -> None:
    """Exports are for downloading, not keeping."""
    if not folder.is_dir():
        return
    cutoff = time.time() - EXPORT_KEEP_SECONDS
    for f in folder.iterdir():
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
