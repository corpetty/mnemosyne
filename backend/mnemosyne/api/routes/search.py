"""Full-text search across sessions."""

from fastapi import APIRouter, Depends, Query

from ...models.search import SearchHit
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=list[SearchHit])
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    ctx: AppContext = Depends(get_ctx),
):
    return ctx.repo.search(q, limit=limit)
