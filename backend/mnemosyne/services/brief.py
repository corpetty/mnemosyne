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
from ..models.session import DEFAULT_SESSION_NAME, ActionItem, NotesItem, SummaryData
from .organizations import OrganizationBrief
from .tasks import TaskItem


@dataclass
class MeetingMeta:
    id: str
    name: str
    created_at: datetime
    attendees: list[str] = field(default_factory=list)
    summary: str = ""
    summary_data: SummaryData | None = None
    notes_items: list[NotesItem] = field(default_factory=list)  # typed in the notes

    def entries(self) -> list[tuple[int, ActionItem, Literal["summary", "notes"]]]:
        """(idx, item, source) of every action item: the summary's, then the notes' (idx is
        -(j+1) for the j-th, as in TaskItem)."""
        summary = self.summary_data.action_items if self.summary_data else []
        return [(i, a, "summary") for i, a in enumerate(summary)] + [
            (-(j + 1), a, "notes") for j, a in enumerate(self.notes_items)
        ]


class RelatedMeeting(ApiModel):
    id: str
    name: str
    created_at: datetime
    match: Literal["title", "people", "both"]


class OpenQuestion(ApiModel):
    session_id: str
    session_name: str
    text: str


class BriefItem(TaskItem):
    task_id: str = ""  # the task object it belongs to ("" = not linked)


class Brief(ApiModel):
    meetings: list[RelatedMeeting]  # newest first
    open_items: list[BriefItem]  # not done, from those meetings; one row per task object
    open_questions: list[OpenQuestion]  # from the most recent summarized one
    last_summary: str  # of the most recent summarized one
    # The organization the meeting is with, and what it said last time (services/organizations.py).
    organization: OrganizationBrief | None = None


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

    # A linked task is one object: its head (newest meeting among all given) says what it is
    # and whether it is done; it is listed once, under its newest related meeting.
    heads: dict[str, tuple[tuple[datetime, str, int], ActionItem]] = {}
    for m in meetings:
        for i, a, _ in m.entries():
            if a.task_id:
                rank = (m.created_at, m.id, i)
                if a.task_id not in heads or rank > heads[a.task_id][0]:
                    heads[a.task_id] = (rank, a)
    items: list[BriefItem] = []
    listed: set[str] = set()
    for m, _ in related:
        for i, mention, source in m.entries():
            a = mention
            if a.task_id:
                if a.task_id in listed:
                    continue
                a = heads[a.task_id][1]
                listed.add(a.task_id)
            if not a.done:
                items.append(
                    BriefItem(
                        session_id=m.id,
                        session_name=m.name,
                        created_at=m.created_at,
                        idx=i,
                        item_id=mention.id,
                        source=source,
                        text=a.text,
                        owner=a.owner,
                        done=False,
                        issue_url=a.issue_url,
                        due=a.due,
                        follow=a.follow,
                        task_id=a.task_id,
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
