"""Comments and threads on a meeting (services/comments.py): on transcript lines, moments of the
audio, summary blocks and summary text."""

from fastapi import APIRouter, Depends, HTTPException

from ...models.comment import Comment, CommentCreate, CommentUpdate, SessionComments
from ...services import comments
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/comments", tags=["comments"])


def _run(fn, *args):
    """Service errors as HTTP: a bad target or thread 400, a missing comment 404 (access.Hidden
    and Forbidden are answered 404 and 403 by the app's handler)."""
    try:
        return fn(*args)
    except comments.BadComment as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except comments.CommentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("", response_model=SessionComments)
async def list_comments(session_id: str, ctx: AppContext = Depends(get_ctx)):
    return _run(comments.threads, ctx, session_id)


@router.post("", response_model=Comment)
async def add_comment(session_id: str, request: CommentCreate, ctx: AppContext = Depends(get_ctx)):
    return _run(comments.create, ctx, session_id, request)


@router.patch("/{comment_id}", response_model=Comment)
async def edit_comment(
    session_id: str, comment_id: str, request: CommentUpdate, ctx: AppContext = Depends(get_ctx)
):
    return _run(comments.edit, ctx, session_id, comment_id, request.text)


@router.delete("/{comment_id}")
async def delete_comment(session_id: str, comment_id: str, ctx: AppContext = Depends(get_ctx)):
    _run(comments.delete, ctx, session_id, comment_id)
    return {"deleted": True}


@router.post("/{comment_id}/resolve", response_model=Comment)
async def resolve_comment(session_id: str, comment_id: str, ctx: AppContext = Depends(get_ctx)):
    return _run(comments.resolve, ctx, session_id, comment_id)


@router.post("/{comment_id}/reopen", response_model=Comment)
async def reopen_comment(session_id: str, comment_id: str, ctx: AppContext = Depends(get_ctx)):
    return _run(comments.reopen, ctx, session_id, comment_id)
