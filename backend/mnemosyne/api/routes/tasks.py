"""Action items across all meetings."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ...models.base import ApiModel
from ...services import organizations
from ...services.brief import Brief, build_brief
from ...services.tasks import TaskItem, filter_tasks
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/action-items", response_model=list[TaskItem])
async def list_action_items(
    status: Literal["open", "done", "all"] = "open",
    owner: str | None = None,
    due: Literal["overdue", "week"] | None = None,
    ctx: AppContext = Depends(get_ctx),
):
    """Action items from every summarized meeting, newest meeting first (open ones with a
    deadline first, soonest first)."""
    return filter_tasks(ctx.repo.list_action_items(), status, owner, due)


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


@router.get("/brief", response_model=Brief)
async def get_brief(
    title: str = "",
    attendees: list[str] = Query(default=[]),
    exclude: str | None = None,
    ctx: AppContext = Depends(get_ctx),
):
    """What is still open from earlier meetings with this title or these people, and what the
    organization the meeting is with said last time."""
    brief = build_brief(ctx.repo.meeting_meta(), title, attendees, exclude)
    brief.organization = organizations.brief(ctx, title, attendees, exclude)
    return brief
