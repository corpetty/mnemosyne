"""Action items across all meetings."""

import asyncio
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ... import access
from ...models.base import ApiModel
from ...models.comment import Comment, CommentThread
from ...services import (
    comments,
    decision_links,
    history,
    item_flags,
    notes_items,
    organizations,
    shared_notes,
    shared_with_me,
    summary_doc,
    task_links,
)
from ...services.brief import Brief, build_brief
from ...services.decision_links import DecisionMention, DecisionObject
from ...services.task_links import TaskMention, TaskObject, TaskSuggestion
from ...services.tasks import TaskItem, filter_tasks, mine_matcher
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/action-items", response_model=list[TaskItem])
async def list_action_items(
    status: Literal["open", "done", "all"] = "open",
    owner: str | None = None,
    due: Literal["overdue", "week"] | None = None,
    mine: bool = False,
    scope: Literal["mine", "following", "everyone"] = "everyone",
    ctx: AppContext = Depends(get_ctx),
):
    """Action items from every meeting the caller can read, newest meeting first (open ones
    with a deadline first, soonest first). `mine`: only those whose owner is the caller.
    `scope`: "mine" (the same), "following" (starred items that are not the caller's) or
    "everyone" (default)."""
    tasks = ctx.repo.list_action_items()
    is_mine = _mine(ctx)
    p = access.principal()
    team = p is not None and bool(p.user_id)
    shared = shared_with_me.shared_ids(ctx.repo)
    for t in tasks:
        if t.session_id in shared:  # someone else's meeting shared with me: tick, never edit
            t.can_edit = False
        # "Me" is the microphone speaker of the meeting: on a team server that is its owner.
        t.mine = is_mine(t.owner, not team or ctx.repo.session_owner(t.session_id) == p.user_id)
    return filter_tasks(tasks, status, owner, due, mine=mine, scope=scope)


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
    """What to change; fields left out stay. Ticking `done` and starring (`follow`) need read
    access only (follow is kept with the meeting, not per person); `text`,
    `owner` and `due` edit the summary and need write access. `owner` "" or null clears it,
    `due` null clears it."""

    done: bool | None = None
    follow: bool | None = None
    text: str | None = None
    owner: str | None = None
    due: date | None = None


@router.patch("/sessions/{session_id}/action-items/{idx}", response_model=TaskItem)
async def update_action_item(
    session_id: str, idx: int, request: ActionItemUpdate, ctx: AppContext = Depends(get_ctx)
):
    if idx < 0:  # TaskItem.idx of a notes item: changed by id, through the notes
        raise HTTPException(
            status_code=400,
            detail="An action item from the notes: PATCH /api/sessions/{id}/notes-items/{item_id}",
        )
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
        body = session.summary or ""
        ctx.repo._check_write(session_id)  # 403 before the doc changes
        try:  # recorded in the summary doc, so a re-summarize keeps the edit
            data, body, _ = summary_doc.apply_human(ctx, session_id, data, body, data.edited_by)
        except summary_doc.SummaryDocUnavailable:
            pass
        ctx.repo.update_fields(session_id, summary=body, summary_data=data)  # write access, or 403
        history.log(ctx, session_id, "task_edited", task=data.action_items[idx].text,
                    by=data.edited_by)  # fmt: skip
        items = data.action_items
    # done and follow belong to the task: a linked item's other mentions follow (item_flags).
    who = access.principal()
    await asyncio.to_thread(
        item_flags.set_item_flags,
        ctx,
        session,
        idx,
        done=request.done if "done" in given else None,
        follow=request.follow if "follow" in given else None,
        by=who.name if who else "",
        item=items[idx],
    )
    a = items[idx]
    return TaskItem(
        session_id=session.id,
        session_name=session.name,
        created_at=session.created_at,
        idx=idx,
        item_id=a.id,
        source="summary",
        text=a.text,
        owner=a.owner,
        done=a.done,
        issue_url=a.issue_url,
        due=a.due,
        follow=a.follow,
        can_edit=access.can_write(session.owner_id) and not session.shared_from,
    )


class NotesItemUpdate(ApiModel):
    """What to change on an action item typed in the notes; fields left out stay. `done` and
    `follow` need read access (done is written into the notes, as a tick there), `due` (null
    clears it) write access. Text and owner change in the notes only."""

    done: bool | None = None
    follow: bool | None = None
    due: date | None = None


@router.patch("/sessions/{session_id}/notes-items/{item_id}", response_model=TaskItem)
async def update_notes_item(
    session_id: str, item_id: str, request: NotesItemUpdate, ctx: AppContext = Depends(get_ctx)
):
    """Tick, star or date an action item from the meeting's notes (services/notes_items.py).
    403 "Tick it in the notes" on a meeting someone shared with me; 409 when the notes could
    not take the tick."""
    given = request.model_fields_set
    who = access.principal()
    kwargs: dict = {"by": who.name if who else ""}
    if "done" in given and request.done is not None:
        kwargs["done"] = request.done
    if "follow" in given and request.follow is not None:
        kwargs["follow"] = request.follow
    if "due" in given:
        kwargs["due"] = request.due
    try:
        j, item = await asyncio.to_thread(notes_items.update, ctx, session_id, item_id, **kwargs)
    except access.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except shared_notes.NotesDocError as e:
        raise HTTPException(status_code=409, detail=f"The notes could not take it: {e}") from None
    session = ctx.sessions.get_session(session_id)
    return TaskItem(
        session_id=session_id,
        session_name=session.name if session else "",
        created_at=session.created_at if session else datetime.now(),
        idx=-(j + 1),
        item_id=item.id,
        source="notes",
        text=item.text,
        owner=item.owner,
        done=item.done,
        issue_url=item.issue_url,
        due=item.due,
        follow=item.follow,
        can_edit=bool(session) and access.can_write(session.owner_id) and not session.shared_from,
    )


# ---- task objects (services/task_links.py) -------------------------------------------------


def _viewer(ctx: AppContext):
    """(is_mine, team, principal) for marking objects with `mine` like the action-item list."""
    p = access.principal()
    return _mine(ctx), p is not None and bool(p.user_id), p


def _present(ctx: AppContext, objs: list[TaskObject]) -> list[TaskObject]:
    is_mine, team, p = _viewer(ctx)
    shared = shared_with_me.shared_ids(ctx.repo)
    for o in objs:
        head = o.mentions[0]
        owner_id = ctx.repo.session_owner(head.session_id)
        o.mine = is_mine(o.owner, not team or owner_id == p.user_id)
        o.can_edit = access.can_write(owner_id or "") and head.session_id not in shared
    return objs


def _object_or_404(ctx: AppContext, key: str) -> TaskObject:
    obj = task_links.index(ctx.repo, ctx.repo.visible_ids()).get(key)
    if obj is None:
        raise HTTPException(status_code=404, detail="No such task")
    return _present(ctx, [obj])[0]


@router.get("/tasks", response_model=list[TaskObject])
async def list_tasks(
    status: Literal["open", "done", "all"] = "open",
    owner: str | None = None,
    due: Literal["overdue", "week"] | None = None,
    mine: bool = False,
    scope: Literal["mine", "following", "everyone"] = "everyone",
    ctx: AppContext = Depends(get_ctx),
):
    """Tasks across meetings: one object per task, with its mentions the caller can read
    (newest first; the first is the head, whose text, owner and due the object shows). Same
    filters as /action-items, applied to the head."""
    objs = _present(
        ctx,
        sorted(
            task_links.index(ctx.repo, ctx.repo.visible_ids()).values(),
            key=lambda o: (o.mentions[0].created_at, o.mentions[0].idx),
            reverse=True,
        ),
    )
    # The head is one item; idx tells old summary items without an id apart (notes: negative).
    rows = {(m.session_id, m.item_id, m.idx): o for o in objs for m in o.mentions[:1]}
    items = [
        TaskItem(
            session_id=o.mentions[0].session_id,
            session_name=o.mentions[0].session_name,
            created_at=o.mentions[0].created_at,
            idx=o.mentions[0].idx,
            item_id=o.mentions[0].item_id,
            source=o.mentions[0].source,
            text=o.text,
            owner=o.owner,
            done=o.done,
            issue_url=o.issue_url,
            due=o.due,
            follow=o.follow,
            mine=o.mine,
            can_edit=o.can_edit,
        )
        for o in objs
    ]
    return [
        rows[(t.session_id, t.item_id, t.idx)]
        for t in filter_tasks(items, status, owner, due, mine=mine, scope=scope)
    ]


class TaskUpdate(ApiModel):
    """Tick or star a whole task: every meeting that mentions it follows."""

    done: bool | None = None
    follow: bool | None = None


class ItemRef(ApiModel):
    session_id: str
    item_id: str


class LinkRequest(ApiModel):
    a: ItemRef
    b: ItemRef


@router.patch("/tasks/{key}", response_model=TaskObject)
async def update_task(key: str, request: TaskUpdate, ctx: AppContext = Depends(get_ctx)):
    _object_or_404(ctx, key)  # 404 when the caller reads no mention of it
    who = access.principal()
    try:  # in a thread: a notes mention's tick runs the notes sidecar (notes_items.write_fields)
        if request.done is not None:
            await asyncio.to_thread(
                task_links.set_done, ctx, key, request.done, by=who.name if who else ""
            )
        if request.follow is not None:
            await asyncio.to_thread(task_links.set_follow, ctx, key, request.follow)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such task") from None
    except shared_notes.NotesDocError as e:
        raise HTTPException(status_code=409, detail=f"The notes could not take it: {e}") from None
    return _object_or_404(ctx, key)


@router.post("/tasks/link", response_model=TaskObject)
async def link_tasks(request: LinkRequest, ctx: AppContext = Depends(get_ctx)):
    """Make two action items one task (or merge their tasks). The head's state wins."""
    a, b = request.a, request.b
    try:
        task_id = task_links.link_pair(ctx, (a.session_id, a.item_id), (b.session_id, b.item_id))
    except access.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return _object_or_404(ctx, task_id)


@router.delete("/tasks/{key}/mentions/{session_id}/{item_id}", response_model=TaskObject)
async def unlink_task_mention(
    key: str, session_id: str, item_id: str, ctx: AppContext = Depends(get_ctx)
):
    """Take one meeting's item out of a task. Returns the item as a task of its own."""
    obj = task_links.object_of(ctx.repo, session_id, item_id, ctx.repo.visible_ids())
    if obj is None or obj.key != key or not obj.linked:
        raise HTTPException(status_code=404, detail="Not part of that task")
    try:
        task_links.unlink(ctx, session_id, item_id)
    except access.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    return _object_or_404(ctx, f"{session_id}:{item_id}")


@router.get("/tasks/suggestions", response_model=list[TaskSuggestion])
async def task_suggestions(ctx: AppContext = Depends(get_ctx)):
    """Open tasks that look like one but are not linked, best match first."""
    return task_links.suggestions(ctx)


@router.post("/tasks/suggestions/apply", response_model=TaskObject)
async def apply_task_suggestion(request: LinkRequest, ctx: AppContext = Depends(get_ctx)):
    return await link_tasks(request, ctx)


@router.post("/tasks/suggestions/dismiss", status_code=204)
async def dismiss_task_suggestion(request: LinkRequest, ctx: AppContext = Depends(get_ctx)):
    """Never suggest these two together again (kept for the whole library)."""
    visible = ctx.repo.visible_ids()
    for ref in (request.a, request.b):
        if task_links.object_of(ctx.repo, ref.session_id, ref.item_id, visible) is None:
            raise HTTPException(status_code=404, detail="No such action item")
    task_links.dismiss(
        ctx, (request.a.session_id, request.a.item_id), (request.b.session_id, request.b.item_id)
    )
    return Response(status_code=204)


class ItemLinks(ApiModel):
    """The same task in other meetings the caller can read: `earlier` ones (newest first) and
    `later` ones (oldest first)."""

    key: str
    earlier: list[TaskMention]
    later: list[TaskMention]


class DecisionLinks(ApiModel):
    """The same decision in other meetings the caller can read: `earlier` ones (newest first)
    and `later` ones (oldest first), as for ItemLinks."""

    key: str
    earlier: list[DecisionMention]
    later: list[DecisionMention]


class SessionLinks(ApiModel):
    items: dict[str, ItemLinks]  # by ActionItem.id (notes items' too)
    decisions: dict[str, DecisionLinks]  # by decision id


@router.get("/sessions/{session_id}/links", response_model=SessionLinks)
async def session_links(session_id: str, ctx: AppContext = Depends(get_ctx)):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    objs = task_links.index(ctx.repo, ctx.repo.visible_ids())
    here = (session.created_at, session.id)
    out: dict[str, ItemLinks] = {}
    summary = session.summary_data.action_items if session.summary_data else []
    for i, a in [*enumerate(summary), *((-(j + 1), n) for j, n in enumerate(session.notes_items))]:
        key = a.task_id or f"{session_id}:{a.id or f'#{i}'}"
        obj = objs.get(key)
        if not a.id or obj is None:
            continue
        others = [m for m in obj.mentions if m.session_id != session_id]
        out[a.id] = ItemLinks(
            key=key,
            earlier=[m for m in others if (m.created_at, m.session_id) < here],
            later=[m for m in others if (m.created_at, m.session_id) >= here][::-1],
        )
    return SessionLinks(items=out, decisions=_decision_links(ctx, session))


def _decision_links(ctx: AppContext, session) -> dict[str, DecisionLinks]:
    data = session.summary_data
    if data is None or not data.decision_ids:
        return {}
    objs = decision_links.index(ctx.repo, ctx.repo.visible_ids())
    here = (session.created_at, session.id)
    out: dict[str, DecisionLinks] = {}
    for decision_id in data.decision_ids:
        key = data.decision_links.get(decision_id) or f"{session.id}:{decision_id}"
        obj = objs.get(key)
        if obj is None:
            continue
        others = [m for m in obj.mentions if m.session_id != session.id]
        out[decision_id] = DecisionLinks(
            key=key,
            earlier=[m for m in others if (m.created_at, m.session_id) < here],
            later=[m for m in others if (m.created_at, m.session_id) >= here][::-1],
        )
    return out


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


# ---- decision objects (services/decision_links.py) -----------------------------------------


class DecisionRef(ApiModel):
    session_id: str
    decision_id: str


class DecisionLinkRequest(ApiModel):
    """Two decisions of two meetings that are one: the newer meeting's wording becomes the
    object's current one (a "supersedes" link when the wording differs)."""

    a: DecisionRef
    b: DecisionRef


def _decision_or_404(ctx: AppContext, key: str) -> DecisionObject:
    obj = decision_links.index(ctx.repo, ctx.repo.visible_ids()).get(key)
    if obj is None:
        raise HTTPException(status_code=404, detail="No such decision")
    return obj


@router.get("/decisions", response_model=list[DecisionObject])
async def list_decisions(linked: bool | None = None, ctx: AppContext = Depends(get_ctx)):
    """Decisions across the meetings the caller can read, one object each (newest head
    first). `linked=true`: only those in more than one meeting; `false`: only the others."""
    objs = sorted(
        decision_links.index(ctx.repo, ctx.repo.visible_ids()).values(),
        key=lambda o: (o.mentions[0].created_at, o.mentions[0].session_id),
        reverse=True,
    )
    return [o for o in objs if linked is None or o.linked == linked]


@router.post("/decisions/link", response_model=DecisionObject)
async def link_decisions(request: DecisionLinkRequest, ctx: AppContext = Depends(get_ctx)):
    """Make two decisions one object (or merge their objects)."""
    a, b = request.a, request.b
    try:
        key = decision_links.link_pair(
            ctx, (a.session_id, a.decision_id), (b.session_id, b.decision_id)
        )
    except access.Forbidden as e:
        raise HTTPException(status_code=403, detail=str(e)) from None
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return _decision_or_404(ctx, key)


@router.delete(
    "/decisions/{key}/mentions/{session_id}/{decision_id}", response_model=DecisionObject
)
async def unlink_decision_mention(
    key: str, session_id: str, decision_id: str, ctx: AppContext = Depends(get_ctx)
):
    """Take one meeting's decision out of an object. Returns it as an object of its own."""
    obj = decision_links.object_of(ctx.repo, session_id, decision_id, ctx.repo.visible_ids())
    if obj is None or obj.key != key or not obj.linked:
        raise HTTPException(status_code=404, detail="Not part of that decision")
    decision_links.unlink(ctx, session_id, decision_id)
    return _decision_or_404(ctx, f"{session_id}:{decision_id}")


# ---- comments on tasks and decisions (services/comments.py) --------------------------------


class MentionComments(ApiModel):
    session_id: str
    session_name: str
    created_at: datetime
    item_id: str  # the action item's or decision's id in that meeting
    threads: list[CommentThread]


class ObjectComments(ApiModel):
    """The threads on a task or decision, grouped by the meeting (mention) they were written on."""

    mentions: list[MentionComments]


class ObjectCommentCreate(ApiModel):
    text: str
    parent_id: str = ""  # reply to this thread (its root comment id)


def _object_comments(ctx: AppContext, kind: str, key: str) -> ObjectComments:
    try:
        return ObjectComments(
            mentions=[MentionComments(**g) for g in comments.for_object(ctx, kind, key)]
        )
    except comments.CommentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


def _post_object_comment(ctx: AppContext, kind: str, key: str, request: ObjectCommentCreate):
    try:
        return comments.post_to_object(ctx, kind, key, request.text, request.parent_id)
    except comments.BadComment as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except comments.CommentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from None


@router.get("/tasks/{key}/comments", response_model=ObjectComments)
async def task_comments(key: str, ctx: AppContext = Depends(get_ctx)):
    return _object_comments(ctx, "task", key)


@router.post("/tasks/{key}/comments", response_model=Comment)
async def add_task_comment(
    key: str, request: ObjectCommentCreate, ctx: AppContext = Depends(get_ctx)
):
    """A new thread lands on the task's head mention; a reply on the thread's own meeting."""
    return _post_object_comment(ctx, "task", key, request)


@router.get("/decisions/{key}/comments", response_model=ObjectComments)
async def decision_comments(key: str, ctx: AppContext = Depends(get_ctx)):
    return _object_comments(ctx, "decision", key)


@router.post("/decisions/{key}/comments", response_model=Comment)
async def add_decision_comment(
    key: str, request: ObjectCommentCreate, ctx: AppContext = Depends(get_ctx)
):
    return _post_object_comment(ctx, "decision", key, request)
