"""Model listing and summarization endpoints."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services.session_service import session_service
from ...services.summarization_service import summarization_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["models"])


class ProviderModels(BaseModel):
    provider: str
    models: list[str]


class SummarizeRequest(BaseModel):
    provider: str = "ollama"
    model: str = ""


class SummarizeResponse(BaseModel):
    summary: str
    provider: str
    model: str


@router.get("/models", response_model=list[ProviderModels])
async def list_models():
    """List available models from all configured providers."""
    return await summarization_service.list_all_models()


@router.post("/sessions/{session_id}/summarize", response_model=SummarizeResponse)
async def summarize_session(session_id: str, request: SummarizeRequest):
    """Generate a summary for a session's transcript."""
    session = session_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.transcript:
        raise HTTPException(status_code=400, detail="Session has no transcript")

    try:
        result = await summarization_service.summarize(
            segments=[seg.model_dump() for seg in session.transcript],
            provider_name=request.provider,
            model=request.model,
        )

        # Save summary to session
        session_service.set_summary(session_id, result["summary"])

        return SummarizeResponse(
            summary=result["summary"],
            provider=result["provider"],
            model=result["model"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Summarization failed")
        raise HTTPException(status_code=500, detail=f"Summarization failed: {e}")
