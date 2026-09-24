"""Action items across all meetings."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from ...models.base import ApiModel
from ...services.tasks import TaskItem, filter_tasks
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/action-items", response_model=list[TaskItem])
async def list_action_items(
    status: Literal["open", "done", "all"] = "open",
    owner: str | None = None,
    ctx: AppContext = Depends(get_ctx),
):
    """Action items from every summarized meeting, newest meeting first."""
    return filter_tasks(ctx.repo.list_action_items(), status, owner)


class ActionItemUpdate(ApiModel):
    done: bool


@router.patch("/sessions/{session_id}/action-items/{idx}", response_model=TaskItem)
async def update_action_item(
    session_id: str, idx: int, request: ActionItemUpdate, ctx: AppContext = Depends(get_ctx)
):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    items = session.summary_data.action_items if session.summary_data else []
    if not 0 <= idx < len(items):
        raise HTTPException(status_code=404, detail="No such action item")
    items[idx].done = request.done
    ctx.repo.update_fields(session_id, summary_data=session.summary_data)
    ctx.bus.publish({"type": "session", "session_id": session_id, "status": session.status.value})
    a = items[idx]
    return TaskItem(
        session_id=session.id,
        session_name=session.name,
        created_at=session.created_at,
        idx=idx,
        text=a.text,
        owner=a.owner,
        done=a.done,
        issue_url=a.issue_url,
    )
