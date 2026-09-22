"""Model listing and summarization endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...models.session import SummaryData
from ...summarization.prompts import STYLES
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["models"])


class ProviderModels(BaseModel):
    provider: str
    models: list[str]


class SummarizeRequest(BaseModel):
    provider: str = ""
    model: str = ""
    style: str = ""  # blank = settings.summary_style
    instructions: str | None = None  # None = settings.summary_instructions


class SummarizeResponse(BaseModel):
    summary: str
    data: SummaryData
    provider: str
    model: str


@router.get("/summary-styles")
async def summary_styles():
    return [{"id": k, "description": v} for k, v in STYLES.items()]


@router.get("/models", response_model=list[ProviderModels])
async def list_models(ctx: AppContext = Depends(get_ctx)):
    return await ctx.summarizer.list_all_models()


@router.post("/sessions/{session_id}/summarize", response_model=SummarizeResponse)
async def summarize_session(
    session_id: str, request: SummarizeRequest, ctx: AppContext = Depends(get_ctx)
):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.transcript:
        raise HTTPException(status_code=400, detail="Session has no transcript")

    provider = request.provider or ctx.settings.default_provider
    model = request.model or ctx.settings.default_model
    style = request.style or ctx.settings.summary_style
    if style not in STYLES:
        raise HTTPException(status_code=400, detail=f"Unknown style; choose one of {list(STYLES)}")
    instructions = (
        request.instructions
        if request.instructions is not None
        else ctx.settings.summary_instructions
    )
    try:
        result = await ctx.summarizer.summarize(
            segments=[seg.model_dump() for seg in session.transcript],
            provider_name=provider,
            model=model,
            style=style,
            instructions=instructions,
        )
        ctx.sessions.set_summary(session_id, result["summary"], result["data"])
        return SummarizeResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Summarization failed")
        raise HTTPException(status_code=500, detail=f"Summarization failed: {e}") from e
