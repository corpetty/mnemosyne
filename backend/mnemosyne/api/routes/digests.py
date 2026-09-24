"""Digests of the meetings in a date range."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.base import ApiModel
from ...models.digest import Digest
from ...services.digest_service import week_bounds
from ...services.pipeline import make_digest
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["digests"])


class DigestRequest(ApiModel):
    start: date | None = None  # default: Monday of the current week
    end: date | None = None  # inclusive; default: start + 6 days
    provider: str = ""
    model: str = ""


@router.post("/digests", response_model=Job)
async def create_digest(request: DigestRequest, ctx: AppContext = Depends(get_ctx)):
    """Queue a `digest` job. Its result is the saved Digest."""
    start = request.start or week_bounds(date.today())[0]
    end = request.end or start + timedelta(days=6)
    if end < start:
        raise HTTPException(status_code=400, detail="end is before start")
    if (end - start).days > 92:
        raise HTTPException(status_code=400, detail="A digest covers at most 93 days")
    return ctx.jobs.submit("digest", make_digest(ctx, start, end, request.provider, request.model))


@router.get("/digests", response_model=list[Digest])
async def list_digests(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    return ctx.repo.list_digests(limit=limit)


@router.get("/digests/{digest_id}", response_model=Digest)
async def get_digest(digest_id: str, ctx: AppContext = Depends(get_ctx)):
    digest = ctx.repo.get_digest(digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Not found")
    return digest


@router.delete("/digests/{digest_id}")
async def delete_digest(digest_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.repo.delete_digest(digest_id):
        raise HTTPException(status_code=404, detail="Not found")
    return {"message": "Deleted"}
