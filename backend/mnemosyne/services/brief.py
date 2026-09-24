"""Pre-meeting brief: what is still open from earlier meetings like this one.

"Like this one" means the same title (ignoring case, punctuation and dates, so a
recurring "Infra weekly" matches every week) or the same people (at least two shared
attendees, or the same one or two people for a 1:1)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Literal

from ..models.base import ApiModel
from ..models.session import DEFAULT_SESSION_NAME, SummaryData
from .tasks import TaskItem


@dataclass
class MeetingMeta:
    id: str
    name: str
    created_at: datetime
    attendees: list[str] = field(default_factory=list)
    summary: str = ""
    summary_data: SummaryData | None = None


class RelatedMeeting(ApiModel):
    id: str
    name: str
    created_at: datetime
    match: Literal["title", "people", "both"]


class OpenQuestion(ApiModel):
    session_id: str
    session_name: str
    text: str


class Brief(ApiModel):
    meetings: list[RelatedMeeting]  # newest first
    open_items: list[TaskItem]  # not done, from those meetings
    open_questions: list[OpenQuestion]  # from the most recent summarized one
    last_summary: str  # of the most recent summarized one


_DATEISH = re.compile(r"\b\d{1,4}([-/.]\d{1,2}){1,2}\b|\b\d+\b")


def norm_title(title: str) -> str:
    t = _DATEISH.sub(" ", title.lower())
    return " ".join(re.findall(r"[^\W\d_]+", t))


_GENERIC = {norm_title(DEFAULT_SESSION_NAME), "meeting", "call", "sync", "chat", "untitled"}


def title_match(a: str, b: str) -> bool:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb or na in _GENERIC or nb in _GENERIC:
        return False
    return na == nb or SequenceMatcher(None, na, nb).ratio() >= 0.88


def _people(names: list[str]) -> set[str]:
    return {n.strip().casefold() for n in names if n.strip()}


def people_match(a: list[str], b: list[str]) -> bool:
    pa, pb = _people(a), _people(b)
    shared = pa & pb
    if len(shared) >= 2:
        return True
    # 1:1s and tiny meetings: the same small group.
    return bool(shared) and pa == pb and len(pa) <= 2


def build_brief(
    meetings: list[MeetingMeta],
    title: str,
    attendees: list[str],
    exclude: str | None = None,
    limit: int = 5,
) -> Brief:
    related: list[tuple[MeetingMeta, str]] = []
    for m in sorted(meetings, key=lambda x: x.created_at, reverse=True):
        if m.id == exclude:
            continue
        by_title = bool(title) and title_match(title, m.name)
        by_people = bool(attendees) and people_match(attendees, m.attendees)
        if by_title or by_people:
            kind = "both" if by_title and by_people else "title" if by_title else "people"
            related.append((m, kind))
        if len(related) >= limit:
            break

    items: list[TaskItem] = []
    for m, _ in related:
        for i, a in enumerate(m.summary_data.action_items if m.summary_data else []):
            if not a.done:
                items.append(
                    TaskItem(
                        session_id=m.id,
                        session_name=m.name,
                        created_at=m.created_at,
                        idx=i,
                        text=a.text,
                        owner=a.owner,
                        done=False,
                        issue_url=a.issue_url,
                    )
                )
    latest = next((m for m, _ in related if m.summary.strip()), None)
    questions = (
        [
            OpenQuestion(session_id=latest.id, session_name=latest.name, text=q)
            for q in latest.summary_data.open_questions
        ]
        if latest and latest.summary_data
        else []
    )
    return Brief(
        meetings=[
            RelatedMeeting(id=m.id, name=m.name, created_at=m.created_at, match=kind)
            for m, kind in related
        ],
        open_items=items,
        open_questions=questions,
        last_summary=latest.summary.strip() if latest else "",
    )
