"""Ask a question across all meetings."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.ask import Ask, Passage
from ...models.base import ApiModel
from ...services.pipeline import ask_question
from ..context import AppContext, get_ctx
from .export import ExportResponse, build_exporter

router = APIRouter(prefix="/api", tags=["ask"])


class AskRequest(ApiModel):
    question: str
    provider: str = ""
    model: str = ""
    # Leave local-only meetings out even with a local provider (the MCP server sets this:
    # its answer goes to an assistant, usually a cloud LLM).
    exclude_local_only: bool = False


@router.post("/ask", response_model=Job)
async def ask(request: AskRequest, ctx: AppContext = Depends(get_ctx)):
    """Queue an `ask` job. Its result is the saved Ask (answer + citations)."""
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty")
    if len(question) > 2000:
        raise HTTPException(status_code=400, detail="Question is too long")
    return ctx.jobs.submit(
        "ask",
        ask_question(ctx, question, request.provider, request.model, request.exclude_local_only),
    )


@router.get("/asks", response_model=list[Ask])
async def list_asks(limit: int = 50, ctx: AppContext = Depends(get_ctx)):
    return ctx.repo.list_asks(limit=limit)


@router.delete("/asks/{ask_id}")
async def delete_ask(ask_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.repo.delete_ask(ask_id):
        raise HTTPException(status_code=404, detail="Not found")
    return {"message": "Deleted"}


@router.get("/ask/passages", response_model=list[Passage])
async def preview_passages(q: str, limit: int = 20, ctx: AppContext = Depends(get_ctx)):
    """What retrieval would feed the model for this question (debugging aid)."""
    return ctx.repo.retrieve(q, limit=limit)


@router.post("/asks/{ask_id}/obsidian", response_model=ExportResponse)
async def save_ask_note(ask_id: str, ctx: AppContext = Depends(get_ctx)):
    """Write the answer to `<vault>/<subfolder>/asks/<date> <question>.md`, with the meetings it
    drew on as [[links]]. 409 when the vault has a note of the user's with that name."""
    from ...export.ask_notes import write_ask_note
    from ...export.obsidian import note_stem
    from ...export.topic_notes import NoteConflict

    st = ctx.settings
    if not st.obsidian_vault_path:
        raise HTTPException(status_code=400, detail="Obsidian vault path not configured")
    vault = Path(st.obsidian_vault_path).expanduser()
    if not vault.is_dir():
        raise HTTPException(status_code=400, detail=f"Vault not found: {vault}")
    ask = ctx.repo.get_ask(ask_id)
    if ask is None:
        raise HTTPException(status_code=404, detail="Not found")

    def run() -> Path:
        stems = {}
        for c in ask.citations:
            s = ctx.repo.get(c.session_id)
            if s is not None:
                stems[c.session_id] = note_stem(s)
        exporter = build_exporter(ctx, st.obsidian_vault_path)
        return write_ask_note(
            vault,
            st.obsidian_subfolder,
            ask,
            stems,
            known_notes=exporter.known_notes,
            redact=st.redact_exports,
        )

    try:
        path = await asyncio.to_thread(run)
    except NoteConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ExportResponse(path=str(path), message="Saved")
