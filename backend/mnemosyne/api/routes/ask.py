"""Ask a question across all meetings."""

from fastapi import APIRouter, Depends, HTTPException

from ...jobs import Job
from ...models.ask import Ask, Passage
from ...models.base import ApiModel
from ...services.pipeline import ask_question
from ..context import AppContext, get_ctx

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
