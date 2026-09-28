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


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def same_item(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.85


def carry_over(old: SummaryData | None, new: SummaryData) -> SummaryData:
    """Copy done flags and issue links from a previous summary onto matching items of
    a new one (re-summarizing must not reopen finished tasks or orphan issues)."""
    if old is None:
        return new
    used: set[int] = set()
    for item in new.action_items:
        for i, prev in enumerate(old.action_items):
            if i not in used and same_item(item.text, prev.text):
                used.add(i)
                item.done = item.done or prev.done
                item.issue_url = item.issue_url or prev.issue_url
                item.due = item.due or prev.due
                break
    return new


def add_live_todos(notes: CopilotNotes | None, data: SummaryData) -> SummaryData:
    """Append the copilot's to-dos that the final summary has no matching item for, marked
    `live`, so nothing agreed during the meeting is lost."""
    if notes is None:
        return data
    for todo in notes.action_items:
        if not any(same_item(todo.text, item.text) for item in data.action_items):
            data.action_items.append(ActionItem(text=todo.text, owner=todo.owner, live=True))
    return data


def filter_tasks(
    tasks: list[TaskItem],
    status: str = "open",
    owner: str | None = None,
    due: str | None = None,
    today: date | None = None,
) -> list[TaskItem]:
    """Filter by done state, owner and deadline (`due`: "overdue" or "week", i.e. due by the
    end of the next seven days). Open tasks with a deadline come first, soonest first."""
    if status == "open":
        tasks = [t for t in tasks if not t.done]
    elif status == "done":
        tasks = [t for t in tasks if t.done]
    if owner:
        want = owner.casefold()
        tasks = [t for t in tasks if (t.owner or "").casefold() == want]
    today = today or date.today()
    if due == "overdue":
        tasks = [t for t in tasks if t.due and t.due < today]
    elif due == "week":
        tasks = [t for t in tasks if t.due and t.due <= today + timedelta(days=7)]
    if status == "open":
        tasks = sorted(tasks, key=lambda t: (t.due is None, t.due or date.max))  # stable
    return tasks
