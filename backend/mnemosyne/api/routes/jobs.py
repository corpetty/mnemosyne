"""Background job inspection. On a team server an advisor sees the jobs they started and
the jobs about their meetings (api/websocket.py `visibility`)."""

from fastapi import APIRouter, Depends, HTTPException

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
    if not job_id or not await ctx.jobs.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job is not running")
    return ctx.jobs.get(job_id)
