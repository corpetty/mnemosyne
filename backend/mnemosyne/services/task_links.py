"""Tasks across meetings: action items linked into one task object.

The link lives on the item (`ActionItem.task_id`, 12 hex, "" = not linked); objects are derived.
A task object is every action item, in any meeting, with the same `task_id`; an unlinked item
is its own object. Object keys: the `task_id`, or `<session_id>:<item id>` for an unlinked item
(`<session_id>:#<index>` for an old item without an id: listed, never linked or fanned out).

State. The head is the mention in the newest meeting (created_at; within a meeting the later
item) among the meetings the caller can see: text, owner, due and at are the head's. done,
follow and issue_url belong to the object: every write fans out to all its mentions, also into
meetings the caller cannot see on a team server (`fan_out_action_item_fields` in the
repository; the history line names who did it). `reconcile` repairs mentions that disagree
(after a sync merge, at startup): the head wins, except that an issue link is never dropped
(the head's, else the newest mention's that has one).

Linking (`link_items`, run after a meeting is summarized): for each item of the meeting without
a link, the candidates are OPEN objects (head not done) with no mention in this meeting, seen in
other meetings the caller can see, whose owner is compatible (same, either side ownerless, or a
person in common in a compound owner), each used once. Tier "exact": the same normalized text
(services/tasks.py `_norm`). Tier "related" adds close text (ratio >= 0.85, both at least 20
normalized characters) when one of the object's meetings is related to this one (services/brief.py
`title_match` or `people_match`). Only objects whose head is in an OLDER meeting (created_at,
then session id) qualify: an item never links forward, so re-summarizing an old meeting cannot
join it to a newer one's task. A done task that comes up again stays a new task. Every link is
a `task_linked` history line. `link_pair` / `unlink` are the manual ones; `suggestions` lists
close pairs on existing data that are not linked or dismissed.

Access: `index(repo, visible)` filters with the set the caller passes (None = all meetings);
every other function here filters with `repo.visible_ids()` of the current principal (None =
no principal = everything). Writing a `task_id` needs read access to the meeting (a reader
links a task they were given, as they may tick it); fan-out of done/follow/issue_url needs
none, provided the caller can see at least one mention of the task."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from .. import access
from ..models.base import ApiModel
from ..models.session import ActionItem
from . import history
from .brief import people_match, title_match
from .tasks import _norm, owner_parts

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..storage.sqlite import SessionRepository

logger = logging.getLogger(__name__)

RELATED_RATIO = 0.85  # services/tasks.py same_item
RELATED_MIN_CHARS = 20  # normalized text, both sides
DISMISSED_META = "task_link_dismissed"  # meta key: JSON [[ref, ref], ...], refs sorted

Ref = tuple[str, str]  # (session_id, item_id)


class TaskMention(ApiModel):
    """One action item of one meeting, as part of a task object."""

    session_id: str
    session_name: str
    created_at: datetime
    idx: int  # position in the meeting's summary_data.action_items
    item_id: str  # ActionItem.id ("" for an old item without one)
    text: str
    owner: str | None
    due: date | None
    at: float | None
    done: bool
    follow: bool
    issue_url: str | None


class TaskObject(ApiModel):
    """A task across meetings: the head mention's text, owner, due and at; the object's done,
    follow and issue_url (the head's; mentions agree after a fan-out or reconcile)."""

    key: str  # task_id, or "<session_id>:<item id>" for an unlinked item
    task_id: str  # "" for an unlinked item
    text: str
    owner: str | None
    due: date | None
    at: float | None
    done: bool
    follow: bool
    issue_url: str | None
    mentions: list[TaskMention]  # visible ones, newest first: mentions[0] is the head
    linked: bool  # more than one visible mention
    # Set by routes/tasks.py for the caller: the head's owner is whoever asks (mine_matcher);
    # whoever asks may edit the head's meeting (done and follow need no write access).
    mine: bool = False
    can_edit: bool = False
    # Open (not resolved, not deleted) comment threads on the visible mentions.
    open_comments: int = 0


class TaskLink(ApiModel):
    """A link `link_items` made: this meeting's item joined the other meeting's task."""

    session_id: str
    item_id: str
    task_id: str
    other_session_id: str
    other_item_id: str  # the head of the task it joined
    tier: Literal["exact", "related"]


class TaskSuggestion(ApiModel):
    """Two open tasks that look like one (related tier), not linked and not dismissed: the
    heads of both, the newer first."""

    a: TaskMention
    b: TaskMention
    score: float  # text similarity, 0.85..1


# ---- reading ------------------------------------------------------------------------------


@dataclass
class _Meeting:
    id: str
    name: str
    created_at: datetime
    owner_id: str
    attendees: list[str]
    items: list[ActionItem] = field(default_factory=list)


def _meetings(repo: SessionRepository) -> list[_Meeting]:
    return [
        _Meeting(
            id=r["id"],
            name=r["name"],
            created_at=r["created_at"],
            owner_id=r["owner_id"],
            attendees=r["attendees"],
            items=r["summary_data"].action_items,
        )
        for r in repo.action_item_sources()
    ]


def _key(session_id: str, item: ActionItem, idx: int) -> str:
    return item.task_id or f"{session_id}:{item.id or f'#{idx}'}"


def _mention(m: _Meeting, idx: int, a: ActionItem) -> TaskMention:
    return TaskMention(
        session_id=m.id,
        session_name=m.name,
        created_at=m.created_at,
        idx=idx,
        item_id=a.id,
        text=a.text,
        owner=a.owner,
        due=a.due,
        at=a.at,
        done=a.done,
        follow=a.follow,
        issue_url=a.issue_url,
    )


def _newest_first(mentions: list[TaskMention]) -> list[TaskMention]:
    return sorted(mentions, key=lambda x: (x.created_at, x.idx, x.session_id), reverse=True)


def _object(key: str, mentions: list[TaskMention]) -> TaskObject:
    mentions = _newest_first(mentions)
    head = mentions[0]
    return TaskObject(
        key=key,
        task_id="" if ":" in key else key,
        text=head.text,
        owner=head.owner,
        due=head.due,
        at=head.at,
        done=head.done,
        follow=head.follow,
        issue_url=head.issue_url,
        mentions=mentions,
        linked=len(mentions) > 1,
    )


def _grouped(meetings: list[_Meeting], visible: set[str] | None) -> dict[str, list[TaskMention]]:
    groups: dict[str, list[TaskMention]] = {}
    for m in meetings:
        if visible is not None and m.id not in visible:
            continue
        for i, a in enumerate(m.items):
            groups.setdefault(_key(m.id, a, i), []).append(_mention(m, i, a))
    return groups


def _objects(meetings: list[_Meeting], visible: set[str] | None) -> dict[str, TaskObject]:
    return {k: _object(k, ms) for k, ms in _grouped(meetings, visible).items()}


def index(repo: SessionRepository, visible: set[str] | None = None) -> dict[str, TaskObject]:
    """Every task object of the summarized meetings in `visible` (None = all meetings; pass
    `repo.visible_ids()` for the caller's view), by key. Mentions in other meetings are left
    out, and the head is the newest of those left."""
    objs = _objects(_meetings(repo), visible)
    _count_comments(repo, objs, visible)
    return objs


def _count_comments(repo: SessionRepository, objs: dict[str, TaskObject], visible) -> None:
    """Fill `open_comments`: one query of the comment roots for all the objects."""

    counts = repo.open_comment_counts(visible)
    for o in objs.values():
        o.open_comments = sum(counts.get((m.session_id, "item", m.item_id), 0) for m in o.mentions)


def object_of(
    repo: SessionRepository, session_id: str, item_id: str, visible: set[str] | None = None
) -> TaskObject | None:
    """The task object an action item (by id) belongs to, or None for no such item."""
    meetings = _meetings(repo)
    found = _find(meetings, session_id, item_id)
    if found is None or (visible is not None and session_id not in visible):
        return None
    m, i, a = found
    key = _key(m.id, a, i)
    objs = _objects(meetings, visible)
    _count_comments(repo, objs, visible)
    return objs.get(key)


def _find(meetings: list[_Meeting], session_id: str, item_id: str):
    if not item_id:
        return None
    for m in meetings:
        if m.id == session_id:
            for i, a in enumerate(m.items):
                if a.id == item_id:
                    return m, i, a
    return None


def _all_mentions(meetings: list[_Meeting], key: str) -> list[TaskMention]:
    """Every mention of a key in every meeting (no access filter)."""
    return _grouped(meetings, None).get(key, [])


# ---- matching rules (pure) ------------------------------------------------------------------


def mint() -> str:
    """A new task id: 12 hex characters, never all digits (identifier redaction)."""
    while (task_id := uuid4().hex[:12]).isdigit():
        pass
    return task_id


def owners_compatible(a: str | None, b: str | None) -> bool:
    """The same owner (any case), either side ownerless, or a person in common when one is
    compound ("Ann and Bob" fits "Ann")."""
    oa, ob = (a or "").strip().casefold(), (b or "").strip().casefold()
    if not oa or not ob or oa == ob:
        return True
    return bool(set(owner_parts(oa)) & set(owner_parts(ob)))


def exact_match(a: str, b: str) -> bool:
    na = _norm(a)
    return bool(na) and na == _norm(b)


def close_score(a: str, b: str) -> float | None:
    """The related tier's text test: the similarity when it is at least RELATED_RATIO and both
    texts have RELATED_MIN_CHARS normalized characters, else None."""
    na, nb = _norm(a), _norm(b)
    if min(len(na), len(nb)) < RELATED_MIN_CHARS:
        return None
    sm = SequenceMatcher(None, na, nb)
    if sm.real_quick_ratio() < RELATED_RATIO or sm.quick_ratio() < RELATED_RATIO:
        return None
    ratio = sm.ratio()
    return ratio if ratio >= RELATED_RATIO else None


def meetings_related(a: _Meeting, b: _Meeting) -> bool:
    """A recurring title or the same people (services/brief.py)."""
    return title_match(a.name, b.name) or people_match(a.attendees, b.attendees)


# ---- writing --------------------------------------------------------------------------------


def _older(a: tuple, b: tuple) -> bool:
    """(created_at, session id) a is strictly before b: the order that decides which meeting an
    item may link back to (ties on the time go by session id, so it is deterministic)."""
    return a < b


def _set_task_id(app: AppContext, session_id: str, item_id: str, task_id: str) -> bool:
    return app.repo.set_action_item_fields_by_id(session_id, item_id, task_id=task_id)


def _notify(app: AppContext, session_ids, acting: str = "") -> None:
    """Tell open windows that these meetings changed (one `session` event each), except the one
    the caller is acting on: its own route answers for it. Never raises."""
    try:
        for sid in sorted(set(session_ids) - {acting}):
            with app.repo._lock:
                row = app.repo._conn.execute(
                    "SELECT status FROM sessions WHERE id=?", (sid,)
                ).fetchone()
            if row is not None:
                app.bus.publish({"type": "session", "session_id": sid, "status": row["status"]})
    except Exception:
        logger.warning("Could not announce task changes", exc_info=True)


def _issue_of(mentions: list[TaskMention]) -> str | None:
    """The head's issue link, else the newest mention's: a link never drops an issue."""
    return next((x.issue_url for x in mentions if x.issue_url), None)


def _settle(app: AppContext, mentions: list[TaskMention], visible: set[str] | None) -> set[str]:
    """Make every mention's done/follow/issue_url the head's (head among `visible`, or among
    all when none is visible). The meetings changed."""
    if len(mentions) < 2:
        return set()
    ordered = _newest_first(mentions)
    seen = [x for x in ordered if visible is None or x.session_id in visible] or ordered
    head = seen[0]
    want = {"done": head.done, "follow": head.follow, "issue_url": _issue_of(seen + ordered)}
    changed: set[str] = set()
    for x in ordered:
        diff = {k: v for k, v in want.items() if getattr(x, k) != v}
        if (
            diff
            and x.item_id
            and app.repo.fan_out_action_item_fields(x.session_id, x.item_id, **diff)
        ):
            changed.add(x.session_id)
    return changed


def link_items(
    app: AppContext, session_id: str, tiers: Literal["exact", "related"] = "exact"
) -> list[TaskLink]:
    """Link the meeting's unlinked items to open tasks of other meetings (rules in the module
    docstring), each candidate used once; exact matches first, then close ones when `tiers` is
    "related". Joining a task makes its mentions agree with its head. Returns the links made."""
    repo = app.repo
    meetings = _meetings(repo)
    by_id = {m.id: m for m in meetings}
    this = by_id.get(session_id)
    if this is None:
        return []
    visible = repo.visible_ids()
    taken = {a.task_id for a in this.items if a.task_id}
    candidates = [
        o
        for o in _objects(meetings, visible).values()
        if not o.done
        and o.mentions[0].item_id
        and o.task_id not in taken
        and all(x.session_id != session_id for x in o.mentions)
        and _older((o.mentions[0].created_at, o.mentions[0].session_id), (this.created_at, this.id))
    ]
    candidates.sort(key=lambda o: (o.mentions[0].created_at, o.mentions[0].idx), reverse=True)
    pending = [(i, a) for i, a in enumerate(this.items) if a.id and not a.task_id]
    used: set[str] = set()
    matched: list[tuple[ActionItem, TaskObject, str]] = []

    for _, a in pending:
        for o in candidates:
            if (
                o.key not in used
                and owners_compatible(a.owner, o.owner)
                and exact_match(a.text, o.text)
            ):
                used.add(o.key)
                matched.append((a, o, "exact"))
                break
    if tiers == "related":
        done_items = {id(a) for a, _, _ in matched}
        related: dict[str, bool] = {}

        def is_related(o: TaskObject) -> bool:
            for sid in {x.session_id for x in o.mentions}:
                if sid not in related:
                    related[sid] = meetings_related(this, by_id[sid])
                if related[sid]:
                    return True
            return False

        for _, a in pending:
            if id(a) in done_items:
                continue
            best: tuple[float, TaskObject] | None = None
            for o in candidates:
                if o.key in used or not owners_compatible(a.owner, o.owner):
                    continue
                score = close_score(a.text, o.text)
                if score is not None and (best is None or score > best[0]) and is_related(o):
                    best = (score, o)
            if best is not None:
                used.add(best[1].key)
                matched.append((a, best[1], "related"))

    links: list[TaskLink] = []
    for a, o, tier in matched:
        head = o.mentions[0]
        task_id = o.task_id
        if not task_id:  # a singleton: its one item gets the new id
            task_id = mint()
            if not _set_task_id(app, head.session_id, head.item_id, task_id):
                continue
        if not _set_task_id(app, session_id, a.id, task_id):
            continue
        a.task_id = task_id
        links.append(
            TaskLink(
                session_id=session_id,
                item_id=a.id,
                task_id=task_id,
                other_session_id=head.session_id,
                other_item_id=head.item_id,
                tier=tier,
            )
        )
        history.log(app, session_id, "task_linked", task=a.text, earlier=head.session_name,
                    session=head.session_id)  # fmt: skip
    if links:
        meetings = _meetings(repo)
        for task_id in {link.task_id for link in links}:
            _settle(app, _all_mentions(meetings, task_id), visible)
    return links


def _visible_mention(
    meetings: list[_Meeting], ref: Ref, visible: set[str] | None
) -> tuple[_Meeting, int, ActionItem]:
    found = _find(meetings, *ref)
    if found is None or (visible is not None and ref[0] not in visible):
        raise LookupError("No such action item")
    return found


def link_pair(app: AppContext, a: Ref, b: Ref, acting: str = "") -> str:
    """Link two action items (session_id, item_id) by hand: neither linked → a new task id;
    one linked → the other joins it; two different tasks → merged (the smaller one's id is
    rewritten on every mention; it must have none in a meeting the caller cannot see, else
    the other is rewritten, and when both do: access.Forbidden). Then the task's
    done/follow/issue_url become its head's, fanned out. Returns the task id. LookupError:
    no such item, or one the caller cannot see; ValueError: the same item twice. Other
    meetings that changed are announced on the bus (not `acting`)."""
    if a == b:
        raise ValueError("An action item cannot be linked to itself")
    repo = app.repo
    meetings = _meetings(repo)
    visible = repo.visible_ids()
    ma, _, ia = _visible_mention(meetings, a, visible)
    mb, _, ib = _visible_mention(meetings, b, visible)
    ta, tb = ia.task_id, ib.task_id
    linked = True
    touched: set[str] = set()
    if ta and ta == tb:
        task_id, linked = ta, False
    elif not ta and not tb:
        task_id = mint()
        _set_task_id(app, ma.id, ia.id, task_id)
        _set_task_id(app, mb.id, ib.id, task_id)
        touched |= {ma.id, mb.id}
    elif not tb:
        task_id = ta
        _set_task_id(app, mb.id, ib.id, task_id)
        touched.add(mb.id)
    elif not ta:
        task_id = tb
        _set_task_id(app, ma.id, ia.id, task_id)
        touched.add(ma.id)
    else:
        mentions = {t: _all_mentions(meetings, t) for t in (ta, tb)}

        def hidden(t: str) -> bool:
            return visible is not None and any(x.session_id not in visible for x in mentions[t])

        loser, task_id = (tb, ta) if len(mentions[tb]) <= len(mentions[ta]) else (ta, tb)
        if hidden(loser):
            loser, task_id = task_id, loser
            if hidden(loser):
                raise access.Forbidden("Both tasks are also in meetings you cannot see")
        for x in mentions[loser]:
            if x.item_id:
                _set_task_id(app, x.session_id, x.item_id, task_id)
                touched.add(x.session_id)
    if linked:
        newer, older = (ma, mb) if (ma.created_at, ma.id) >= (mb.created_at, mb.id) else (mb, ma)
        text = (ia if newer is ma else ib).text
        history.log(app, newer.id, "task_linked", task=text, earlier=older.name,
                    session=older.id)  # fmt: skip
    touched |= _settle(app, _all_mentions(_meetings(repo), task_id), visible)
    _notify(app, touched, acting)
    return task_id


def unlink(app: AppContext, session_id: str, item_id: str, acting: str = "") -> bool:
    """Take an item out of its task. When only one mention is left, that one is unlinked too
    (no task of one); a last mention in a meeting the caller cannot see keeps its id, which
    is harmless. False: not linked, or no such visible item."""
    repo = app.repo
    meetings = _meetings(repo)
    try:
        _, _, item = _visible_mention(meetings, (session_id, item_id), repo.visible_ids())
    except LookupError:
        return False
    task_id = item.task_id
    if not task_id or not _set_task_id(app, session_id, item_id, ""):
        return False
    rest = [
        x
        for x in _all_mentions(meetings, task_id)
        if (x.session_id, x.item_id) != (session_id, item_id)
    ]
    touched = {session_id}
    if len(rest) == 1 and rest[0].item_id:
        _set_task_id(app, rest[0].session_id, rest[0].item_id, "")
        touched.add(rest[0].session_id)
    _notify(app, touched, acting)
    return True


def _fan_out(app: AppContext, key: str, **values) -> tuple[list[TaskMention], TaskMention]:
    """Write `values` to every mention of the task `key`. The mentions changed and the head
    (among the visible ones). LookupError when the caller can see no mention of it."""
    meetings = _meetings(app.repo)
    visible = app.repo.visible_ids()
    mentions = _newest_first(_all_mentions(meetings, key))
    seen = [x for x in mentions if visible is None or x.session_id in visible]
    if not seen:
        raise LookupError("No such task")
    changed = []
    for x in mentions:
        if any(getattr(x, k) != v for k, v in values.items()) and x.item_id:
            if app.repo.fan_out_action_item_fields(x.session_id, x.item_id, **values):
                changed.append(x)
    return changed, seen[0]


def set_done(app: AppContext, key: str, done: bool, by: str = "", acting: str = "") -> int:
    """Tick a task done (or reopen it) in every meeting that mentions it. Logs "task_done" /
    "task_reopened" (task, by) in each changed meeting, except the head's when the caller owns
    it (as routes/tasks.py does for a tick). Other changed meetings are announced on the bus
    (not `acting`, the one the caller is in). Returns the number of meetings changed."""
    changed, head = _fan_out(app, key, done=done)
    me = access.user_id()
    owners = {m.id: m.owner_id for m in _meetings(app.repo)} if changed else {}
    for x in changed:
        if x.session_id != head.session_id or owners.get(x.session_id, "") != me:
            history.log(app, x.session_id, "task_done" if done else "task_reopened",
                        task=x.text, by=by)  # fmt: skip
    _notify(app, {x.session_id for x in changed}, acting)
    return len({x.session_id for x in changed})


def set_follow(app: AppContext, key: str, follow: bool, acting: str = "") -> int:
    """Star a task (or not) in every meeting that mentions it. Meetings changed."""
    changed, _ = _fan_out(app, key, follow=follow)
    _notify(app, {x.session_id for x in changed}, acting)
    return len({x.session_id for x in changed})


def set_issue_url(app: AppContext, key: str, url: str | None, acting: str = "") -> int:
    """Put an issue link on every mention of a task. Meetings changed."""
    changed, _ = _fan_out(app, key, issue_url=url or None)
    _notify(app, {x.session_id for x in changed}, acting)
    return len({x.session_id for x in changed})


def reconcile(app: AppContext) -> int:
    """Make every linked task's mentions agree with its head (newest meeting of all), e.g.
    after a sync merge brought in a tick for an older mention, or at startup. Returns the
    number of meetings changed."""
    groups: dict[str, list[TaskMention]] = {}
    for key, mentions in _grouped(_meetings(app.repo), None).items():
        if ":" not in key and len(mentions) > 1:
            groups[key] = mentions
    changed: set[str] = set()
    for mentions in groups.values():
        changed |= _settle(app, mentions, None)
    return len(changed)


# ---- the summary model's hints ----------------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    """An open task of an earlier related meeting, shown to the summary model by `label`."""

    label: str  # "T1".."T30": the object key (task id, "<session>:<item>") is too long to ask for
    session_id: str  # the head mention
    item_id: str
    text: str
    owner: str | None
    meeting: str  # the head's meeting name


OFFER_LIMIT = 30


def _visible_to(app: AppContext, owner_id: str) -> set[str] | None:
    """The meetings the owner of a meeting would see: theirs and those shared with them (team
    server), else None = all. The job runs as whoever started it, who may see more or less."""
    if not (app.settings.team_mode and owner_id):
        return None
    repo = app.repo
    with repo._lock:
        rows = repo._conn.execute(
            f"SELECT id FROM sessions WHERE {repo._SEES}", (owner_id, owner_id)
        ).fetchall()
    return {r["id"] for r in rows}


def open_task_offers(
    app: AppContext, session_id: str, limit: int = OFFER_LIMIT, skip_local_only: bool = False
) -> list[Offer]:
    """Open tasks of earlier related meetings (`meetings_related`: recurring title or same
    people) as the meeting's owner would see them, newest first, at most `limit`, labelled T1,
    T2... The meeting's own tasks are left out. `skip_local_only`: leave out meetings marked
    local-only (their text must not reach a cloud model). Never raises: [] on a failure."""
    try:
        repo = app.repo
        meetings = _meetings(repo)
        by_id = {m.id: m for m in meetings}
        this = by_id.get(session_id)
        if this is None:  # not summarized yet: its own row has no summary_data
            session = app.sessions.get_session(session_id)
            if session is None:
                return []
            this = _Meeting(
                session.id, session.name, session.created_at, session.owner_id, session.attendees
            )
        banned: set[str] = set()
        if skip_local_only:
            with repo._lock:
                banned = {
                    r["id"]
                    for r in repo._conn.execute("SELECT id FROM sessions WHERE local_only=1")
                }
        visible = _visible_to(app, this.owner_id)
        related: dict[str, bool] = {}

        def is_related(sid: str) -> bool:
            if sid not in related:
                related[sid] = sid in by_id and meetings_related(this, by_id[sid])
            return related[sid]

        objects = [
            o
            for o in index(repo, visible).values()
            if not o.done
            and o.mentions[0].item_id
            and all(x.session_id != session_id for x in o.mentions)
            and _older(
                (o.mentions[0].created_at, o.mentions[0].session_id), (this.created_at, this.id)
            )
            and o.mentions[0].session_id not in banned
            and is_related(o.mentions[0].session_id)
        ]
        objects.sort(key=lambda o: (o.mentions[0].created_at, o.mentions[0].idx), reverse=True)
        return [
            Offer(
                label=f"T{n}",
                session_id=o.mentions[0].session_id,
                item_id=o.mentions[0].item_id,
                text=o.text,
                owner=o.owner,
                meeting=o.mentions[0].session_name,
            )
            for n, o in enumerate(objects[:limit], start=1)
        ]
    except Exception:
        logger.warning("Could not list open tasks for %s", session_id, exc_info=True)
        return []


def hints_from_items(items: list[ActionItem], offers: list[Offer]) -> dict[str, Ref]:
    """What the summary model said continues what: normalized item text -> the head (session
    id, item id) of the offered task. A label that was not offered is ignored."""
    by_label = {o.label.casefold(): o for o in offers}
    hints: dict[str, Ref] = {}
    for a in items:
        label = a._continues.strip().casefold()
        if not label:
            continue
        offer = by_label.get(label)
        if offer is None:
            logger.debug("Ignoring unknown task label %r on %r", a._continues, a.text)
            continue
        hints.setdefault(_norm(a.text), (offer.session_id, offer.item_id))
    return hints


def apply_hints(app: AppContext, session_id: str, hints: dict[str, Ref]) -> list[str]:
    """Link the meeting's unlinked items whose normalized text is in `hints` to the offered
    task (`link_pair`), when that task is still open, has no mention in this meeting and its
    owner fits; each offered task once. Anything else is skipped, never raised. Returns the
    ids of the items linked."""
    if not hints:
        return []
    repo = app.repo
    this = next((m for m in _meetings(repo) if m.id == session_id), None)
    if this is None:
        return []
    done: list[str] = []
    used: set[Ref] = set()
    for a in this.items:
        ref = hints.get(_norm(a.text))
        if not a.id or a.task_id or ref is None or ref in used:
            continue
        try:
            obj = object_of(repo, ref[0], ref[1], repo.visible_ids())
            if (
                obj is None
                or obj.done
                or any(x.session_id == session_id for x in obj.mentions)
                or not owners_compatible(a.owner, obj.owner)
            ):
                logger.debug("Hint for %r not applied", a.text)
                continue
            head = obj.mentions[0]
            link_pair(app, (session_id, a.id), (head.session_id, head.item_id), acting=session_id)
            used.add(ref)
            done.append(a.id)
        except Exception:
            logger.debug("Hint for %r failed", a.text, exc_info=True)
    return done


# ---- suggestions ----------------------------------------------------------------------------


def _ref_key(session_id: str, item_id: str) -> str:
    return f"{session_id}:{item_id}"


def _dismissed(repo: SessionRepository) -> set[frozenset[str]]:
    try:
        pairs = json.loads(repo.get_meta(DISMISSED_META) or "[]")
    except ValueError:
        return set()
    return {frozenset(p) for p in pairs if isinstance(p, list) and len(p) == 2}


def suggestions(app: AppContext) -> list[TaskSuggestion]:
    """Pairs of open tasks the caller can see that the related tier would link (close text,
    owners compatible, related meetings) but are not linked: never in one meeting together,
    not dismissed (no mention pair of the two was). Best match first."""
    repo = app.repo
    meetings = _meetings(repo)
    by_id = {m.id: m for m in meetings}
    visible = repo.visible_ids()
    dismissed = _dismissed(repo)
    objs = [
        o
        for o in _objects(meetings, visible).values()
        if not o.done and o.mentions[0].item_id and len(_norm(o.text)) >= RELATED_MIN_CHARS
    ]
    sessions = [{x.session_id for x in o.mentions} for o in objs]
    related: dict[frozenset[str], bool] = {}

    def is_related(sa: set[str], sb: set[str]) -> bool:
        for x in sa:
            for y in sb:
                pair = frozenset((x, y))
                if pair not in related:
                    related[pair] = meetings_related(by_id[x], by_id[y])
                if related[pair]:
                    return True
        return False

    out: list[TaskSuggestion] = []
    for i, oa in enumerate(objs):
        for j in range(i + 1, len(objs)):
            ob = objs[j]
            if sessions[i] & sessions[j] or not owners_compatible(oa.owner, ob.owner):
                continue
            score = close_score(oa.text, ob.text)
            if score is None or not is_related(sessions[i], sessions[j]):
                continue
            refs_a = [_ref_key(x.session_id, x.item_id) for x in oa.mentions]
            refs_b = [_ref_key(x.session_id, x.item_id) for x in ob.mentions]
            if any(frozenset((x, y)) in dismissed for x in refs_a for y in refs_b):
                continue
            ha, hb = oa.mentions[0], ob.mentions[0]
            if (ha.created_at, ha.session_id) < (hb.created_at, hb.session_id):
                ha, hb = hb, ha  # a is the newer meeting's task, b the older it would join
            out.append(TaskSuggestion(a=ha, b=hb, score=round(score, 3)))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def dismiss(app: AppContext, a: Ref, b: Ref) -> None:
    """Never suggest these two items (or the tasks they are in) together again. Kept in the
    `meta` table for the whole library."""
    pair = sorted((_ref_key(*a), _ref_key(*b)))
    have = _dismissed(app.repo)
    if frozenset(pair) in have:
        return
    stored = [sorted(p) for p in have] + [pair]
    app.repo.set_meta(DISMISSED_META, json.dumps(stored))
