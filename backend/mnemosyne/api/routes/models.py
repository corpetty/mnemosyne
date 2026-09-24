"""Model listing and summarization endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.base import ApiModel
from ...services.pipeline import summarize_session as summarize_runner
from ...summarization.prompts import STYLES
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["models"])


class ProviderModels(ApiModel):
    provider: str
    models: list[str]


class SummarizeRequest(ApiModel):
    provider: str = ""
    model: str = ""
    style: str = ""  # blank = settings.summary_style
    instructions: str | None = None  # None = settings.summary_instructions


class SummaryStyle(ApiModel):
    id: str
    description: str


@router.get("/summary-styles", response_model=list[SummaryStyle])
async def summary_styles():
    return [{"id": k, "description": v} for k, v in STYLES.items()]


@router.get("/models", response_model=list[ProviderModels])
async def list_models(ctx: AppContext = Depends(get_ctx)):
    return await ctx.summarizer.list_all_models()


@router.post("/sessions/{session_id}/summarize", response_model=Job)
async def summarize_session(
    session_id: str, request: SummarizeRequest, ctx: AppContext = Depends(get_ctx)
):
    """Queue a summarize job. The summary arrives via `job` and `session` events."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.transcript:
        raise HTTPException(status_code=400, detail="Session has no transcript")
    style = request.style or ctx.settings.summary_style
    if style not in STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style; choose one of {list(STYLES)}")
    if any(j.kind == "summarize" for j in ctx.jobs.list(session_id=session_id, active_only=True)):
        raise HTTPException(status_code=409, detail="A summary is already being generated")
    return ctx.jobs.submit(
        "summarize",
        summarize_runner(
            ctx, session_id, request.provider, request.model, style, request.instructions
        ),
        session_id=session_id,
    )
