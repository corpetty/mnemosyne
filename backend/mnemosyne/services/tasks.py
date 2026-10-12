"""Action items across meetings: listing, done state, and keeping that state when a
meeting is summarized again."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from ..models.base import ApiModel
from ..models.session import ActionItem, CopilotNotes, SummaryData


class TaskItem(ApiModel):
    session_id: str
    session_name: str
    created_at: datetime
    idx: int  # position in the meeting's summary_data.action_items
    text: str
    owner: str | None
    done: bool
    issue_url: str | None
    due: date | None = None
    follow: bool = False  # starred: someone else's item the reader waits on
    mine: bool = False  # its owner is whoever asks (mine_matcher)
    # Whoever asks may change its text, owner and due date (their meeting, or an admin);
    # anyone who can read it may tick it done. Set by the task list and the edit route.
    can_edit: bool = False


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def same_item(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.85


def carry_over(old: SummaryData | None, new: SummaryData) -> SummaryData:
    """Copy done and follow flags and issue links from a previous summary onto matching items of
    a new one (re-summarizing must not reopen finished tasks or orphan issues)."""
    if old is None:
        return new
    used: set[int] = set()
    for item in new.action_items:
        for i, prev in enumerate(old.action_items):
            if i not in used and same_item(item.text, prev.text):
                used.add(i)
                item.done = item.done or prev.done
                item.follow = item.follow or prev.follow
                item.issue_url = item.issue_url or prev.issue_url
                item.due = item.due or prev.due
                item.id = item.id or prev.id
                break
    return new


def recorder_names(app, session) -> tuple[list[str], list[str]]:
    """Who recorded a meeting, as (their names and email, everyone else's names): its owner on
    a team server, else `local_speaker_name`. Both empty when nobody is known."""
    owner = getattr(session, "owner_id", "") if session is not None else ""
    users = getattr(app, "users", None)
    if owner and users is not None:
        me = users.get(owner)
        if me is not None:
            names = [me.name] + ([me.email] if getattr(me, "email", "") else [])
            return [n for n in names if n and n.strip()], [
                u.name for u in users.list() if u.id != owner
            ]
    name = (app.settings.local_speaker_name or "").strip()
    return ([name] if name else []), []


def add_live_todos(notes: CopilotNotes | None, data: SummaryData, mine=None) -> SummaryData:
    """Append the copilot's to-dos that the final summary has no matching item for, marked
    `live`, so nothing agreed during the meeting is lost. With `mine` (mine_matcher for the
    person who recorded) only their to-dos and ownerless ones are added; others' stay in the
    copilot notes."""
    if notes is None:
        return data
    for todo in notes.action_items:
        if mine is not None and (todo.owner or "").strip() and not mine(todo.owner, True):
            continue
        if not any(same_item(todo.text, item.text) for item in data.action_items):
            data.action_items.append(ActionItem(text=todo.text, owner=todo.owner, live=True))
    return data


_SPLIT = re.compile(r"\s+and\s+|\s*[&,/]\s*", re.IGNORECASE)
_SELF = {"me", "myself", "i"}


def owner_parts(owner: str) -> list[str]:
    """A compound owner ("Jonny Zerah and Me", "Ann, Bob", "Ann/Bob") as its people."""
    return [p.strip() for p in _SPLIT.split(owner) if p.strip()]


def rename_owner(owner: str | None, old: str, new: str) -> str | None:
    """`owner` with the person `old` (exact, any case) written as `new`, also inside a
    compound owner. Unchanged when `old` is not in it."""
    if not owner or not old.strip():
        return owner
    want = old.strip().casefold()
    if owner.strip().casefold() == want:
        return new
    parts = owner_parts(owner)
    if len(parts) < 2 or want not in {p.casefold() for p in parts}:
        return owner
    return " and ".join(new if p.casefold() == want else p for p in parts)


def mine_matcher(names: list[str], others: list[str]):
    """Is an action item's owner this person? Their full name or email (`names`), or their
    first name when nobody else (`others`: everyone else's names) has it, or "me"/"myself"/"I"
    (the summarizer writes those from the microphone speaker's side; pass `me=False` for a
    meeting where that is somebody else). Owners are written as people said them, so "Ann" is
    Ann Lee unless there is also an Ann Ortiz. A compound owner ("Ann and Me") is mine when
    any of its people is."""
    full = {n.strip().casefold() for n in names if n.strip()}
    taken = {o.split()[0].casefold() for o in others if o.split()}
    first = {n.split()[0].casefold() for n in names if n.split() and "@" not in n}
    short = first - taken

    def one(o: str, me: bool) -> bool:
        return o in full or o in short or (me and o in _SELF)

    def mine(owner: str | None, me: bool = True) -> bool:
        o = (owner or "").strip().casefold()
        if not o:
            return False
        if one(o, me):
            return True
        return any(one(p.casefold(), me) for p in owner_parts(o))

    return mine


def filter_tasks(
    tasks: list[TaskItem],
    status: str = "open",
    owner: str | None = None,
    due: str | None = None,
    today: date | None = None,
    mine: bool = False,
    scope: str = "everyone",
) -> list[TaskItem]:
    """Filter by done state, owner, deadline (`due`: "overdue" or "week", i.e. due by the
    end of the next seven days) and `scope`: "mine" (owner is the caller), "following" (starred
    items that are not the caller's) or "everyone". `mine=True` is the same as scope "mine".
    Open tasks with a deadline come first, soonest first."""
    if status == "open":
        tasks = [t for t in tasks if not t.done]
    elif status == "done":
        tasks = [t for t in tasks if t.done]
    if owner:
        want = owner.casefold()
        tasks = [t for t in tasks if (t.owner or "").casefold() == want]
    if mine or scope == "mine":
        tasks = [t for t in tasks if t.mine]
    elif scope == "following":
        tasks = [t for t in tasks if t.follow and not t.mine]
    today = today or date.today()
    if due == "overdue":
        tasks = [t for t in tasks if t.due and t.due < today]
    elif due == "week":
        tasks = [t for t in tasks if t.due and t.due <= today + timedelta(days=7)]
    if status == "open":
        tasks = sorted(tasks, key=lambda t: (t.due is None, t.due or date.max))  # stable
    return tasks
