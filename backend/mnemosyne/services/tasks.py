"""Action items across meetings: listing, done state, and keeping that state when a
meeting is summarized again."""

from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher

from ..models.base import ApiModel
from ..models.session import SummaryData


class TaskItem(ApiModel):
    session_id: str
    session_name: str
    created_at: datetime
    idx: int  # position in the meeting's summary_data.action_items
    text: str
    owner: str | None
    done: bool
    issue_url: str | None


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
                break
    return new


def filter_tasks(
    tasks: list[TaskItem], status: str = "open", owner: str | None = None
) -> list[TaskItem]:
    if status == "open":
        tasks = [t for t in tasks if not t.done]
    elif status == "done":
        tasks = [t for t in tasks if t.done]
    if owner:
        want = owner.casefold()
        tasks = [t for t in tasks if (t.owner or "").casefold() == want]
    return tasks
