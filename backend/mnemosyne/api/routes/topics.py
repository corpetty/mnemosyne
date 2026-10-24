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
from .export import ExportResponse, build_exporter, task_predicate

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


class TopicNoteRequest(ApiModel):
    query: str
    explanation: str = ""  # the "Where it stands" text the panel already has


@router.post("/obsidian", response_model=ExportResponse)
async def save_topic_note(request: TopicNoteRequest, ctx: AppContext = Depends(get_ctx)):
    """Write the topic's thread (and `explanation`, when given) to `<vault>/<subfolder>/topics/`.
    409 when the vault has a note of the user's with that name."""
    from pathlib import Path

    from ...export.obsidian import note_stem
    from ...export.topic_notes import NoteConflict, write_topic_note

    st = ctx.settings
    if not st.obsidian_vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")
    q = request.query.strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Topic is too short")
    vault = Path(st.obsidian_vault_path).expanduser()
    if not vault.is_dir():
        raise HTTPException(status_code=400, detail=f"Vault not found: {vault}")

    def run() -> Path:
        from ...services.shared_with_me import excluded

        thread = build_thread(ctx.repo, ctx.index, q)
        skip = excluded(ctx, "shared_in_obsidian")  # meetings shared with me: only when asked
        if skip:
            thread = thread.model_copy(
                update={"meetings": [m for m in thread.meetings if m.id not in skip]}
            )
        if not thread.meetings:
            raise ValueError(f'No meetings found about "{q}"')
        stems, predicates = {}, {}
        for m in thread.meetings:
            s = ctx.repo.get(m.id)
            if s is None:
                continue
            stems[m.id] = note_stem(s)
            if st.obsidian_task_scope == "mine":
                predicates[m.id] = (s, task_predicate(ctx, s))

        def is_mine(sid: str, idx: int) -> bool:
            s, pred = predicates.get(sid, (None, None))
            items = s.summary_data.action_items if s is not None and s.summary_data else []
            return pred is not None and idx < len(items) and pred(items[idx])

        exporter = build_exporter(ctx, st.obsidian_vault_path)
        return write_topic_note(
            vault,
            st.obsidian_subfolder,
            thread,
            stems,
            request.explanation,
            known_notes=exporter.known_notes,
            is_mine=is_mine if st.obsidian_task_scope == "mine" else (lambda sid, idx: True),
            redact=st.redact_exports,
        )

    try:
        path = await asyncio.to_thread(run)
    except NoteConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return ExportResponse(path=str(path), message="Saved")
