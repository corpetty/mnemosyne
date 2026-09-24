"""Search across sessions: keyword (FTS5) plus, in hybrid mode, matches by meaning."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ...models.search import SearchHit
from ...search.hybrid import hybrid_search
from ...search.index import IndexStatus
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search", response_model=list[SearchHit])
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    mode: Literal["hybrid", "keyword"] = "hybrid",
    ctx: AppContext = Depends(get_ctx),
):
    if mode == "keyword":
        return ctx.repo.search(q, limit=limit)
    return await asyncio.to_thread(hybrid_search, ctx.repo, ctx.index, q, limit)


@router.get("/search/index", response_model=IndexStatus)
async def index_status(ctx: AppContext = Depends(get_ctx)):
    return ctx.index.status()


@router.post("/search/index/rebuild", response_model=IndexStatus)
async def rebuild_index(ctx: AppContext = Depends(get_ctx)):
    """Drop every vector and re-embed all meetings in the background."""
    await ctx.index.rebuild()
    return ctx.index.status()
