"""People across meetings: who spoke, who was invited, who owns what."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from ..models.base import ApiModel
from ..models.session import SummaryData
from .stats import meeting_stats
from .tasks import TaskItem

_GENERIC = re.compile(r"^(speaker[ _]?\d*|unknown|me|remote|you|them|other)$", re.I)


def is_person(name: str | None, extra_generic: tuple[str, ...] = ()) -> bool:
    if not name or not name.strip():
        return False
    n = name.strip()
    return not _GENERIC.match(n) and n.casefold() not in {g.casefold() for g in extra_generic}


class PersonSummary(ApiModel):
    name: str
    meetings: int
    last_seen: datetime | None
    open_tasks: int
    has_voice: bool


class PersonMeeting(ApiModel):
    id: str
    name: str
    created_at: datetime
    role: Literal["speaker", "invited", "both"]
    talk_seconds: float | None  # when they spoke
    share: float | None


class PersonDecision(ApiModel):
    session_id: str
    session_name: str
    created_at: datetime
    text: str


class PersonDetail(ApiModel):
    name: str
    has_voice: bool
    meetings: list[PersonMeeting]  # newest first
    total_talk_seconds: float
    open_tasks: list[TaskItem]
    done_tasks: list[TaskItem]
    decisions: list[PersonDecision]  # from the last 10 meetings they spoke in


@dataclass
class _Acc:
    names: dict[str, int] = field(default_factory=dict)  # spellings -> count
    speaker_in: set[str] = field(default_factory=set)
    invited_to: set[str] = field(default_factory=set)
    last_seen: datetime | None = None
    tasks: list[TaskItem] = field(default_factory=list)
    has_voice: bool = False

    @property
    def display(self) -> str:
        # Most used spelling; on a tie prefer one with capitals ("Alice" over "alice").
        return max(self.names.items(), key=lambda kv: (kv[1], kv[0] != kv[0].lower()))[0]


def _collect(repo, generic: tuple[str, ...]) -> tuple[dict[str, _Acc], dict[str, dict]]:
    people: dict[str, _Acc] = {}
    meta: dict[str, dict] = {}

    def acc(name: str) -> _Acc:
        key = name.strip().casefold()
        a = people.setdefault(key, _Acc())
        a.names[name.strip()] = a.names.get(name.strip(), 0) + 1
        return a

    for r in repo.people_rows():
        created = datetime.fromisoformat(r["created_at"])
        data = SummaryData.model_validate_json(r["summary_data"]) if r["summary_data"] else None
        meta[r["id"]] = {"name": r["name"], "created_at": created, "data": data}
        for n in json.loads(r["participants"] or "[]"):
            if is_person(n, generic):
                a = acc(n)
                a.speaker_in.add(r["id"])
                a.last_seen = max(filter(None, [a.last_seen, created]))
        for n in json.loads(r["attendees"] or "[]"):
            if is_person(n, generic):
                a = acc(n)
                a.invited_to.add(r["id"])
                a.last_seen = max(filter(None, [a.last_seen, created]))
        for i, item in enumerate(data.action_items if data else []):
            if is_person(item.owner, generic):
                acc(item.owner).tasks.append(
                    TaskItem(
                        session_id=r["id"],
                        session_name=r["name"],
                        created_at=created,
                        idx=i,
                        text=item.text,
                        owner=item.owner,
                        done=item.done,
                        issue_url=item.issue_url,
                    )
                )
    for p in repo.list_speakers():
        if is_person(p.name, generic):
            acc(p.name).has_voice = True
    return people, meta


def list_people(repo, generic: tuple[str, ...] = ()) -> list[PersonSummary]:
    people, _ = _collect(repo, generic)
    out = [
        PersonSummary(
            name=a.display,
            meetings=len(a.speaker_in | a.invited_to),
            last_seen=a.last_seen,
            open_tasks=sum(1 for t in a.tasks if not t.done),
            has_voice=a.has_voice,
        )
        for a in people.values()
    ]
    out.sort(key=lambda p: (p.last_seen or datetime.min, p.meetings), reverse=True)
    return out


def person_detail(repo, name: str, generic: tuple[str, ...] = ()) -> PersonDetail | None:
    people, meta = _collect(repo, generic)
    a = people.get(name.strip().casefold())
    if a is None:
        return None
    ids = sorted(a.speaker_in | a.invited_to, key=lambda i: meta[i]["created_at"], reverse=True)
    spellings = {n.casefold() for n in a.names}
    meetings: list[PersonMeeting] = []
    total = 0.0
    for sid in ids:
        talk = share = None
        if sid in a.speaker_in:
            session = repo.get(sid)
            if session is not None and session.transcript:
                for sp in meeting_stats(session.transcript).speakers:
                    if sp.speaker.casefold() in spellings:
                        talk, share = sp.talk_seconds, sp.share
                        total += sp.talk_seconds
        role = (
            "both"
            if sid in a.speaker_in and sid in a.invited_to
            else "speaker"
            if sid in a.speaker_in
            else "invited"
        )
        m = meta[sid]
        meetings.append(
            PersonMeeting(
                id=sid,
                name=m["name"],
                created_at=m["created_at"],
                role=role,
                talk_seconds=talk,
                share=share,
            )
        )
    decisions = []
    for sid in [i for i in ids if i in a.speaker_in][:10]:
        m = meta[sid]
        for d in m["data"].decisions if m["data"] else []:
            decisions.append(
                PersonDecision(
                    session_id=sid, session_name=m["name"], created_at=m["created_at"], text=d
                )
            )
    tasks = sorted(a.tasks, key=lambda t: t.created_at, reverse=True)
    return PersonDetail(
        name=a.display,
        has_voice=a.has_voice,
        meetings=meetings,
        total_talk_seconds=round(total, 1),
        open_tasks=[t for t in tasks if not t.done],
        done_tasks=[t for t in tasks if t.done],
        decisions=decisions,
    )
