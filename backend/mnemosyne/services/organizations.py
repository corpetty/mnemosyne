"""Organizations: the people you meet grouped by who they are with (a client, a customer, a
partner), with what was said across all of the organization's meetings.

An organization is a name and its members (people by name, as transcripts and calendars name
them, with an email when known). A meeting is the organization's when a member spoke in it or was
invited (by name or email), when its title names a member (first and last name) or the
organization, or when it was sent to the organization's HubSpot company. Its client facts (the
`client` summary style) add up per organization, newest first, and the pre-meeting brief shows
the last meeting's. Facts come only from meetings the caller may see (access.py).

With HubSpot connected, companies and their contacts become organizations; members added by hand
stay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from ..models.base import ApiModel
from ..models.session import ClientFactKind, SummaryData
from ..transcription.mentions import keyword_pattern
from .tasks import TaskItem

if TYPE_CHECKING:
    from ..api.context import AppContext


class OrganizationMember(ApiModel):
    name: str
    email: str = ""
    source: Literal["manual", "hubspot"] = "manual"


class Organization(ApiModel):
    id: str
    name: str
    hubspot_company_id: str
    members: list[OrganizationMember]


class OrganizationInput(ApiModel):
    name: str
    members: list[OrganizationMember]


class OrganizationSummary(Organization):
    meetings: int
    last_meeting: datetime | None
    facts: int


class OrganizationMeeting(ApiModel):
    id: str
    name: str
    created_at: datetime


class OrganizationFact(ApiModel):
    kind: ClientFactKind
    text: str
    at: float | None  # seconds into the meeting
    session_id: str
    session_name: str
    created_at: datetime


class OrganizationDetail(Organization):
    meetings: list[OrganizationMeeting]  # newest first
    facts: list[OrganizationFact]  # newest meeting first, in the order said
    open_tasks: list[TaskItem]


class OrganizationBrief(ApiModel):
    """For the next meeting: what the organization said at the last one that recorded facts."""

    id: str
    name: str
    last_meeting: OrganizationMeeting | None
    facts: list[OrganizationFact]
    earlier_facts: int  # in meetings before that one (on the organization's page)


class HubSpotSync(ApiModel):
    created: int
    updated: int
    organizations: int


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def load(app: AppContext) -> list[Organization]:
    return [Organization(**h) for h in app.repo.organizations()]


def get(app: AppContext, organization_id: str) -> Organization | None:
    return next((h for h in load(app) if h.id == organization_id), None)


def _clean(members: list[OrganizationMember]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for m in members:
        name = " ".join(m.name.split())
        if name and _key(name) not in seen:
            seen.add(_key(name))
            out.append({"name": name, "email": m.email.strip().lower(), "source": m.source})
    return out


def save(
    app: AppContext, data: OrganizationInput, organization_id: str | None = None
) -> Organization:
    old = get(app, organization_id) if organization_id else None
    hid = old.id if old else uuid4().hex[:8]
    app.repo.save_organization(
        hid,
        " ".join(data.name.split()) or "Organization",
        _clean(data.members),
        old.hubspot_company_id if old else "",
    )
    return get(app, hid)  # type: ignore[return-value]


@dataclass
class _Row:
    """A meeting as matching needs it, parsed once for every organization."""

    id: str
    name: str
    created_at: str
    names: set[str]  # speakers and attendees, as keys
    emails: set[str]
    associated: set[str]  # HubSpot records it was pushed to ("companies:900")
    raw_summary: str | None

    @cached_property
    def data(self) -> SummaryData | None:
        return SummaryData.model_validate_json(self.raw_summary) if self.raw_summary else None


def _load_rows(app: AppContext, exclude: str | None = None) -> list[_Row]:
    """The meetings the caller may see, newest first."""
    out = []
    for r in app.repo.people_rows():
        if r["id"] == exclude:
            continue
        attendees = json.loads(r["attendees"] or "[]")
        emails = [a for a in attendees if "@" in a]
        emails += list(json.loads(r["attendee_emails"] or "{}").values())
        crm = json.loads(r["crm"] or "{}").get("hubspot") or {}
        out.append(
            _Row(
                id=r["id"],
                name=r["name"],
                created_at=r["created_at"],
                names={_key(n) for n in json.loads(r["participants"] or "[]") + attendees},
                emails={e.strip().lower() for e in emails},
                associated=set(crm.get("associated", [])),
                raw_summary=r["summary_data"],
            )
        )
    return out


class _Matcher:
    """Does a meeting (its people, emails, title, CRM links) belong to this organization?"""

    def __init__(self, h: Organization):
        self.organization = h
        self.names = {_key(m.name) for m in h.members}
        self.emails = {m.email for m in h.members if m.email}
        # Titles: full names only ("Dana Reyes", not "Dana"), and the organization's name.
        titled = [m.name for m in h.members if len(m.name.split()) >= 2] + [h.name]
        self.titles = [keyword_pattern(t) for t in titled if t.strip()]
        self.company = f"companies:{h.hubspot_company_id}" if h.hubspot_company_id else ""

    def people(self, names: list[str], emails: list[str]) -> int:
        return len({_key(n) for n in names} & self.names) + len(
            {e.strip().lower() for e in emails} & self.emails
        )

    def title(self, title: str) -> bool:
        return any(p.search(title) for p in self.titles)

    def row(self, r: _Row) -> bool:
        return bool(
            r.names & self.names
            or r.emails & self.emails
            or self.title(r.name)
            or (self.company and self.company in r.associated)
        )


def _meeting(r: _Row) -> OrganizationMeeting:
    return OrganizationMeeting(id=r.id, name=r.name, created_at=r.created_at)


def _facts(r: _Row) -> list[OrganizationFact]:
    return [
        OrganizationFact(
            kind=f.kind,
            text=f.text,
            at=f.at,
            session_id=r.id,
            session_name=r.name,
            created_at=r.created_at,
        )
        for f in (r.data.client_facts if r.data else [])
    ]


def _rows(app: AppContext, h: Organization, exclude: str | None = None) -> list[_Row]:
    """The organization's meetings the caller may see, newest first."""
    match = _Matcher(h)
    return [r for r in _load_rows(app, exclude) if match.row(r)]


def summaries(app: AppContext) -> list[OrganizationSummary]:
    rows = _load_rows(app)
    out = []
    for h in load(app):
        match = _Matcher(h)
        mine = [r for r in rows if match.row(r)]
        out.append(
            OrganizationSummary(
                **h.model_dump(),
                meetings=len(mine),
                last_meeting=mine[0].created_at if mine else None,
                facts=sum(len(r.data.client_facts) for r in mine if r.data),
            )
        )
    return out


def detail(app: AppContext, organization_id: str) -> OrganizationDetail | None:
    h = get(app, organization_id)
    if h is None:
        return None
    rows = _rows(app, h)
    tasks = [
        TaskItem(
            session_id=r.id,
            session_name=r.name,
            created_at=r.created_at,
            idx=i,
            text=a.text,
            owner=a.owner,
            done=a.done,
            issue_url=a.issue_url,
            due=a.due,
        )
        for r in rows
        for i, a in enumerate(r.data.action_items if r.data else [])
        if not a.done
    ]
    return OrganizationDetail(
        **h.model_dump(),
        meetings=[_meeting(r) for r in rows],
        facts=[f for r in rows for f in _facts(r)],
        open_tasks=tasks,
    )


def for_meeting(app: AppContext, title: str, attendees: list[str]) -> Organization | None:
    """The organization a coming meeting is with: the most members among its attendees, else one
    its title names."""
    emails = [a for a in attendees if "@" in a]
    best, score = None, 0
    for h in load(app):
        m = _Matcher(h)
        n = m.people(attendees, emails) * 2 + (1 if title and m.title(title) else 0)
        if n > score:
            best, score = h, n
    return best


def brief(
    app: AppContext, title: str, attendees: list[str], exclude: str | None = None
) -> OrganizationBrief | None:
    h = for_meeting(app, title, attendees)
    if h is None:
        return None
    rows = _rows(app, h, exclude)
    with_facts = [r for r in rows if r.data and r.data.client_facts]
    last = with_facts[0] if with_facts else None
    return OrganizationBrief(
        id=h.id,
        name=h.name,
        last_meeting=_meeting(last) if last else None,
        facts=_facts(last) if last else [],
        earlier_facts=sum(len(r.data.client_facts) for r in with_facts[1:] if r.data),
    )


async def sync_hubspot(app: AppContext) -> HubSpotSync:
    """HubSpot companies with contacts become organizations (matched by company id); their
    HubSpot members are replaced, members added by hand stay. Nothing is written to HubSpot."""
    from .hubspot import client_for

    companies = await client_for(app).companies_with_contacts()
    existing = {h.hubspot_company_id: h for h in load(app) if h.hubspot_company_id}
    created = updated = 0
    for company_id, name, contacts in companies:
        old = existing.get(company_id)
        hubspot = [OrganizationMember(name=n, email=e, source="hubspot") for n, e in contacts]
        manual = [m for m in (old.members if old else []) if m.source == "manual"]
        taken = {_key(m.name) for m in hubspot}
        members = _clean(hubspot + [m for m in manual if _key(m.name) not in taken])
        app.repo.save_organization(old.id if old else uuid4().hex[:8], name, members, company_id)
        if old:
            updated += 1
        else:
            created += 1
    return HubSpotSync(created=created, updated=updated, organizations=len(companies))
