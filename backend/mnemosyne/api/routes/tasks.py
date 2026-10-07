"""Action items across all meetings."""

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import access
from ...models.base import ApiModel
from ...services import history, organizations
from ...services.brief import Brief, build_brief
from ...services.tasks import TaskItem, filter_tasks, mine_matcher
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/action-items", response_model=list[TaskItem])
async def list_action_items(
    status: Literal["open", "done", "all"] = "open",
    owner: str | None = None,
    due: Literal["overdue", "week"] | None = None,
    mine: bool = False,
    ctx: AppContext = Depends(get_ctx),
):
    """Action items from every meeting the caller can read, newest meeting first (open ones
    with a deadline first, soonest first). `mine`: only those whose owner is the caller."""
    tasks = ctx.repo.list_action_items()
    is_mine = _mine(ctx)
    for t in tasks:
        t.mine = is_mine(t.owner)
    return filter_tasks(tasks, status, owner, due, mine=mine)


def _mine(ctx: AppContext):
    """Whose action items are the caller's: on a team server by their name and email, on the
    desktop app by the name on the microphone (local_speaker_name)."""
    p = access.principal()
    if p is None or not p.user_id:
        return mine_matcher([ctx.settings.local_speaker_name], [])
    me = ctx.users.get(p.user_id)
    names = [p.name] + ([me.email] if me and me.email else [])
    others = [u.name for u in ctx.users.list() if u.id != p.user_id]
    return mine_matcher(names, others)


class ActionItemUpdate(ApiModel):
    """What to change; fields left out stay. Ticking `done` needs read access only; `text`,
    `owner` and `due` edit the summary and need write access. `owner` "" or null clears it,
    `due` null clears it."""

    done: bool | None = None
    text: str | None = None
    owner: str | None = None
    due: date | None = None


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
    given = request.model_fields_set
    if given & {"text", "owner", "due"}:
        updates: dict = {}
        if "text" in given:
            text = (request.text or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="An action item needs some text")
            updates["text"] = text
        if "owner" in given:
            updates["owner"] = (request.owner or "").strip() or None
        if "due" in given:
            updates["due"] = request.due
        who = access.principal()
        data = session.summary_data.model_copy(deep=True)
        data.action_items[idx] = data.action_items[idx].model_copy(update=updates)
        data.edited_at, data.edited_by = datetime.now(), who.name if who else ""
        ctx.repo.update_fields(session_id, summary_data=data)  # write access, or 403
        history.log(ctx, session_id, "task_edited", task=data.action_items[idx].text,
                    by=data.edited_by)  # fmt: skip
        items = data.action_items
    if "done" in given and request.done is not None and request.done != items[idx].done:
        items[idx].done = request.done
        ctx.repo.set_action_item_done(session_id, idx, request.done)  # readers may tick too
        _log_tick(ctx, session, items[idx].text, request.done)
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
        due=a.due,
        can_edit=access.can_write(session.owner_id),
    )


def _log_tick(ctx: AppContext, session, task: str, done: bool) -> None:
    """Someone other than the owner ticking an item off shows in the meeting's history."""
    if session.owner_id != access.user_id():
        who = access.principal()
        history.log(
            ctx, session.id, "task_done" if done else "task_reopened",
            task=task, by=who.name if who else "",
        )  # fmt: skip


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
