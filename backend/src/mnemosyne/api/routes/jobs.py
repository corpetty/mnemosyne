"""Background job inspection."""

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[Job])
async def list_jobs(
    session_id: str | None = None,
    active_only: bool = False,
    ctx: AppContext = Depends(get_ctx),
):
    return ctx.jobs.list(session_id=session_id, active_only=active_only)


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: str, ctx: AppContext = Depends(get_ctx)):
    job = ctx.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str, ctx: AppContext = Depends(get_ctx)):
    if not await ctx.jobs.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job is not running")
    return ctx.jobs.get(job_id)
