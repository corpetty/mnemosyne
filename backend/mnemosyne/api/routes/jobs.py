"""Background job inspection. On a team server a member sees the jobs they started and
the jobs about meetings they may read (api/websocket.py `visibility`)."""

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...jobs import Job
from ..context import AppContext, get_ctx
from ..websocket import visibility

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[Job])
async def list_jobs(
    session_id: str | None = None,
    active_only: bool = False,
    ctx: AppContext = Depends(get_ctx),
):
    visible = visibility(ctx)
    jobs = ctx.jobs.list(session_id=session_id, active_only=active_only)
    return [j for j in jobs if visible({"job": j.model_dump()})]


def _job(ctx: AppContext, job_id: str) -> Job:
    job = ctx.jobs.get(job_id)
    if job is None or not visibility(ctx)({"job": job.model_dump()}):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: str, ctx: AppContext = Depends(get_ctx)):
    return _job(ctx, job_id)


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str, ctx: AppContext = Depends(get_ctx)):
    job = ctx.jobs.get(job_id)
    if job is not None and not visibility(ctx)({"job": job.model_dump()}):
        job_id = ""  # someone else's: as if it did not exist
    elif job is not None and job.owner_id != access.user_id():
        # About a meeting shared with them: reading it does not let them stop its work.
        owner = ctx.repo.owner_of(job.session_id) if job.session_id else None
        if not access.can_write(owner or ""):
            raise access.Forbidden("Only whoever started it or the meeting's owner can stop it")
    if not job_id or not await ctx.jobs.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job is not running")
    return ctx.jobs.get(job_id)
