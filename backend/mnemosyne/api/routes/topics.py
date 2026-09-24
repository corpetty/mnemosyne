"""Topic threads across meetings."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from ...jobs import Job, JobContext
from ...models.base import ApiModel
from ...services.topics import (
    THREAD_PROMPT,
    Thread,
    TopicCount,
    build_thread,
    frequent_topics,
    thread_notes,
)
from ...summarization.privacy import is_cloud
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/topics", tags=["topics"])


@router.get("", response_model=list[TopicCount])
async def list_topics(limit: int = 30, ctx: AppContext = Depends(get_ctx)):
    """Topics from meeting summaries, most meetings first."""
    return frequent_topics(ctx.repo, limit)


@router.get("/thread", response_model=Thread)
async def get_thread(q: str = Query(..., min_length=2), ctx: AppContext = Depends(get_ctx)):
    """Meetings about `q` (oldest first) with their matching chapters, decisions, items and
    open questions."""
    return await asyncio.to_thread(build_thread, ctx.repo, ctx.index, q)


class ThreadSummaryRequest(ApiModel):
    q: str
    provider: str = ""
    model: str = ""


@router.post("/thread/summary", response_model=Job)
async def summarize_thread(request: ThreadSummaryRequest, ctx: AppContext = Depends(get_ctx)):
    """Queue a `thread` job: the LLM writes where the topic stands. Result: `{text}`."""
    q = request.q.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Topic is too short")

    async def run(job: JobContext) -> dict:
        thread = await asyncio.to_thread(build_thread, ctx.repo, ctx.index, q)
        if not thread.meetings:
            raise ValueError(f'No meetings found about "{q}"')
        st = ctx.settings
        prov = request.provider or st.default_provider
        mdl = await ctx.summarizer.resolve_model(prov, request.model or st.default_model)
        if is_cloud(prov):
            hidden = ctx.repo.local_only_ids()
            thread.meetings = [m for m in thread.meetings if m.id not in hidden]
            if not thread.meetings:
                raise ValueError(f'Only local-only meetings are about "{q}"')
        job.update(f"Reading {len(thread.meetings)} meetings with {prov}/{mdl}")
        text = await ctx.summarizer.complete(THREAD_PROMPT, thread_notes(thread), prov, mdl)
        return {"text": text.strip(), "query": q, "meetings": len(thread.meetings)}

    return ctx.jobs.submit("thread", run)
