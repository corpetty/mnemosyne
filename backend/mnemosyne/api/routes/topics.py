"""Topic threads across meetings."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import access
from ...config import save_settings
from ...jobs import Job, JobContext
from ...models.base import ApiModel
from ...services.topics import (
    THREAD_PROMPT,
    Thread,
    TopicCount,
    alias_for,
    build_thread,
    frequent_topics,
    rename_topic,
    thread_notes,
)
from ...summarization.privacy import is_cloud
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/topics", tags=["topics"])


@router.get("", response_model=list[TopicCount])
async def list_topics(limit: int = 30, ctx: AppContext = Depends(get_ctx)):
    """Topics from meeting summaries, most meetings first."""
    return frequent_topics(ctx.repo, limit)


class RenameTopic(ApiModel):
    old: str
    new: str  # an existing topic's name merges the two


class RenameResult(ApiModel):
    meetings: int  # meetings whose topics changed
    remembered: bool  # kept for new summaries (settings.topic_aliases; admins on a team server)


@router.post("/rename", response_model=RenameResult)
async def rename(request: RenameTopic, ctx: AppContext = Depends(get_ctx)):
    """Rename a topic, or merge it into another, across meetings."""
    old, new = request.old.strip(), " ".join(request.new.split())
    if not old or not new:
        raise HTTPException(status_code=400, detail="Give the topic and its new name")
    meetings = await asyncio.to_thread(rename_topic, ctx, old, new)
    remembered = access.is_admin()
    if remembered:
        aliases = alias_for(ctx.settings.topic_aliases, old, new)
        updated = ctx.settings.model_copy(update={"topic_aliases": aliases})
        save_settings(updated)
        await ctx.apply_settings(updated)
    return RenameResult(meetings=meetings, remembered=remembered)


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
