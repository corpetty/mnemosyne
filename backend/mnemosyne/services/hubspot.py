"""HubSpot: a meeting's record in the CRM, for teams that keep their clients there.

A meeting is matched to HubSpot contacts (calendar attendees by email, then speaker names), the
user confirms which contacts it belongs to, and a push writes one meeting engagement (the
summary), one note (decisions, open questions, client facts) and one task per action item, all
associated with those contacts and each contact's primary company. The ids of what was created
are kept per session (sessions.crm), so pushing again updates the same records instead of
duplicating them.

Auth is a private app's access token with the scopes crm.objects.contacts.read and .write
(meetings, notes and tasks come under the contacts scopes), crm.objects.companies.read and
crm.objects.owners.read. Endpoints are CRM API v3 (supported at least until September 2027,
when HubSpot's date-versioned paths take over); associating existing records uses v4.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal

import httpx

from ..models.base import ApiModel
from ..models.session import ActionItem, Session
from .people import is_person
from .trackers import TrackerCheck

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

API = "https://api.hubapi.com"

# Default (HUBSPOT_DEFINED) association type ids, from
# https://developers.hubspot.com/docs/guides/api/crm/associations/associations-v4
# (also used in the examples of .../engagements/meetings, /notes and /tasks).
MEETING_TO_CONTACT = 200
MEETING_TO_COMPANY = 188
NOTE_TO_CONTACT = 202
NOTE_TO_COMPANY = 190
TASK_TO_CONTACT = 204
TASK_TO_COMPANY = 192
ASSOCIATION_TYPES = {
    ("meetings", "contacts"): MEETING_TO_CONTACT,
    ("meetings", "companies"): MEETING_TO_COMPANY,
    ("notes", "contacts"): NOTE_TO_CONTACT,
    ("notes", "companies"): NOTE_TO_COMPANY,
    ("tasks", "contacts"): TASK_TO_CONTACT,
    ("tasks", "companies"): TASK_TO_COMPANY,
}
# Object type ids for v4 association paths, which only take names for some objects
# (https://developers.hubspot.com/docs/guides/api/crm/understanding-the-crm).
OBJECT_TYPE_IDS = {
    "contacts": "0-1",
    "companies": "0-2",
    "meetings": "0-47",
    "notes": "0-46",
    "tasks": "0-27",
}
CONTACT_PROPERTIES = ["firstname", "lastname", "email", "company", "associatedcompanyid"]
SEARCH_LIMIT = 5  # contacts per name searched
# The search endpoint allows five filter groups per request (OR-ed).
SEARCH_GROUPS = 5


class HubSpotError(RuntimeError):
    pass


class HubSpotContact(ApiModel):
    id: str
    name: str
    email: str = ""
    company: str = ""
    company_id: str | None = None
    # How it was found: by an attendee's email, by a speaker's or attendee's name, or it is
    # one already confirmed for this meeting.
    matched_by: Literal["email", "name", "confirmed"] = "confirmed"
    matched_on: str = ""


class HubSpotState(ApiModel):
    """What a meeting became in HubSpot (stored in sessions.crm under "hubspot")."""

    contacts: list[HubSpotContact] = []  # confirmed by the user
    meeting_id: str | None = None
    note_id: str | None = None
    task_ids: list[str] = []  # by action item index
    # "contacts:1" / "companies:9": associated with every object above.
    associated: list[str] = []
    pushed_at: datetime | None = None


class HubSpotMatches(ApiModel):
    candidates: list[HubSpotContact]  # confirmed ones first, then email, then name matches
    searched: list[str]  # the emails and names looked up
    state: HubSpotState


class HubSpotPushResult(ApiModel):
    created: bool  # first push (False: the records were updated)
    message: str
    state: HubSpotState


def load_state(app: AppContext, session_id: str) -> HubSpotState:
    raw = app.repo.crm_state(session_id, "hubspot")
    return HubSpotState.model_validate(raw) if raw else HubSpotState()


def save_state(app: AppContext, session_id: str, state: HubSpotState) -> None:
    app.repo.set_crm_state(session_id, "hubspot", state.model_dump(mode="json"))


# ---- content -----------------------------------------------------------


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)


def markdown_html(text: str) -> str:
    """The summary's markdown as the simple HTML HubSpot's rich text fields show: paragraphs,
    headings as bold lines, bullet lists, bold and italics."""
    out: list[str] = []
    items: list[str] = []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        if m := re.match(r"^(?:[-*•]|\d{1,2}[.)])\s+(.*)$", s):
            items.append(_inline(m.group(1)))
            continue
        flush()
        if m := re.match(r"^#{1,6}\s+(.*)$", s):
            out.append(f"<p><strong>{_inline(m.group(1))}</strong></p>")
        else:
            out.append(f"<p>{_inline(s)}</p>")
    flush()
    return "".join(out)


def _section(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    body = "".join(f"<li>{html.escape(x, quote=False)}</li>" for x in lines)
    return f"<p><strong>{html.escape(title, quote=False)}</strong></p><ul>{body}</ul>"


def _fact_text(fact) -> str:
    """A client fact as text, whether the model gives strings or objects with `text`."""
    if isinstance(fact, str):
        return fact
    text = getattr(fact, "text", None) or (fact.get("text") if isinstance(fact, dict) else None)
    return str(text or fact)


def note_html(session: Session) -> str:
    data = session.summary_data
    if data is None:
        return ""
    facts = [_fact_text(f) for f in getattr(data, "client_facts", None) or []]
    return "".join(
        [
            _section("Client facts", [f for f in facts if f.strip()]),
            _section("Decisions", data.decisions),
            _section("Open questions", data.open_questions),
        ]
    )


def meeting_times(session: Session) -> tuple[datetime, datetime]:
    """Start (when the session was created, local time) and end (start + the transcript's
    length on the meeting's timeline)."""
    start = session.created_at.astimezone()  # naive = local
    seconds = max((s.end for s in session.transcript), default=0.0)
    return start, start + timedelta(seconds=seconds)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _task_due(item: ActionItem, start: datetime) -> str:
    """hs_timestamp is the due date and is required: the item's deadline at noon local time,
    or when the meeting took place for an item without one."""
    if item.due:
        return _iso(datetime.combine(item.due, time(12)).astimezone())
    return _iso(start)


def _subject(item: ActionItem) -> str:
    return item.text if len(item.text) <= 120 else item.text[:117] + "..."


def _task_body(session: Session, item: ActionItem, start: datetime) -> str:
    lines = [html.escape(item.text, quote=False)]
    if item.owner:
        lines.append(f"Owner in the meeting: {html.escape(item.owner, quote=False)}")
    when = start.strftime("%Y-%m-%d")
    lines.append(f"From “{html.escape(session.name, quote=False)}” on {when}")
    return "<br>".join(lines)


def _name(props: dict) -> str:
    return " ".join(p for p in (props.get("firstname"), props.get("lastname")) if p).strip()


# ---- API ---------------------------------------------------------------


def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        body = None
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("message") or body.get("error") or "")
    if not detail:
        detail = r.text[:200]
    if r.status_code == 401:
        return "HubSpot rejected the access token (check it in Settings)"
    if r.status_code == 403:
        return f"The HubSpot token is missing a scope: {detail}"
    if r.status_code == 429:
        return "HubSpot's rate limit was hit; try again in a few seconds"
    return f"HubSpot {r.status_code}: {detail}"


class HubSpotClient:
    name = "hubspot"

    def __init__(self, token: str, owner_email: str = "", transport=None, base_url: str = API):
        self.token = token.strip()
        self.owner_email = owner_email.strip()
        self._transport = transport
        self.base_url = base_url

    def validate(self) -> str | None:
        if not self.token:
            return "Set a HubSpot private app access token in Settings"
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=20,
            transport=self._transport,
        )

    @staticmethod
    async def _call(c: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
        try:
            r = await c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise HubSpotError(f"Could not reach HubSpot: {e}") from e
        if r.status_code >= 400:
            err = HubSpotError(_error(r))
            err.status = r.status_code  # type: ignore[attr-defined]
            raise err
        return r.json() if r.content else {}

    # -- owner and check

    async def _owner(self, c: httpx.AsyncClient) -> dict | None:
        if not self.owner_email:
            return None
        body = await self._call(
            c, "GET", "/crm/v3/owners", params={"email": self.owner_email, "limit": 100}
        )
        want = self.owner_email.casefold()
        for o in body.get("results", []):
            if str(o.get("email", "")).casefold() == want:
                return o
        raise HubSpotError(f"No HubSpot user with the email {self.owner_email}")

    async def check(self) -> TrackerCheck:
        if problem := self.validate():
            return TrackerCheck(ok=False, message=problem)
        try:
            async with self._client() as c:
                await self._call(c, "GET", "/crm/v3/objects/contacts", params={"limit": 1})
                owner = await self._owner(c)
        except HubSpotError as e:
            return TrackerCheck(ok=False, message=str(e))
        if owner is None:
            return TrackerCheck(
                ok=True, message="Ready (tasks are unassigned: set the owner email to assign them)"
            )
        who = _name({"firstname": owner.get("firstName"), "lastname": owner.get("lastName")})
        return TrackerCheck(
            ok=True, message=f"Ready: tasks are assigned to {who or owner['email']}"
        )

    # -- contacts

    async def _search(self, c: httpx.AsyncClient, groups: list[dict], limit: int) -> list[dict]:
        body = await self._call(
            c,
            "POST",
            "/crm/v3/objects/contacts/search",
            json={"filterGroups": groups, "properties": CONTACT_PROPERTIES, "limit": limit},
        )
        return body.get("results", [])

    async def _company_names(self, c: httpx.AsyncClient, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        try:
            body = await self._call(
                c,
                "POST",
                "/crm/v3/objects/companies/batch/read",
                json={"properties": ["name"], "inputs": [{"id": i} for i in sorted(ids)]},
            )
        except HubSpotError:
            # Without the companies scope the contact's own "company" text still names it.
            logger.info("Could not read HubSpot companies", exc_info=True)
            return {}
        return {r["id"]: (r.get("properties") or {}).get("name") or "" for r in body["results"]}

    async def companies_with_contacts(
        self, limit: int = 5000
    ) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """(company id, name, [(contact name, email)]) for every company with contacts, from
        each contact's primary company (organizations, services/organizations.py). Reads only."""
        if problem := self.validate():
            raise HubSpotError(problem)
        by_company: dict[str, list[tuple[str, str]]] = {}
        async with self._client() as c:
            after, seen = None, 0
            while seen < limit:
                params = {"limit": 100, "properties": ",".join(CONTACT_PROPERTIES)}
                if after:
                    params["after"] = after
                body = await self._call(c, "GET", "/crm/v3/objects/contacts", params=params)
                for r in body.get("results", []):
                    p = r.get("properties") or {}
                    cid, name = p.get("associatedcompanyid"), _name(p)
                    if cid and name:
                        by_company.setdefault(str(cid), []).append((name, p.get("email") or ""))
                seen += len(body.get("results", []))
                after = ((body.get("paging") or {}).get("next") or {}).get("after")
                if not after:
                    break
            ids = sorted(by_company)
            names: dict[str, str] = {}
            for i in range(0, len(ids), 100):  # batch reads take 100 at a time
                names |= await self._company_names(c, set(ids[i : i + 100]))
        return [
            (cid, names.get(cid) or f"HubSpot company {cid}", contacts)
            for cid, contacts in by_company.items()
        ]

    async def _contacts(self, c, rows: list[tuple[dict, str, str]]) -> list[HubSpotContact]:
        """(search result, matched_by, matched_on) rows as contacts with company names."""
        company_ids = {
            str(p["associatedcompanyid"])
            for r, _, _ in rows
            if (p := r.get("properties") or {}).get("associatedcompanyid")
        }
        names = await self._company_names(c, company_ids)
        out = []
        for r, by, on in rows:
            p = r.get("properties") or {}
            cid = str(p["associatedcompanyid"]) if p.get("associatedcompanyid") else None
            out.append(
                HubSpotContact(
                    id=str(r["id"]),
                    name=_name(p) or p.get("email") or f"Contact {r['id']}",
                    email=p.get("email") or "",
                    company=(names.get(cid) if cid else None) or p.get("company") or "",
                    company_id=cid,
                    matched_by=by,  # type: ignore[arg-type]
                    matched_on=on,
                )
            )
        return out

    async def find_contacts(
        self, emails: list[str], names: list[str]
    ) -> tuple[list[HubSpotContact], list[str]]:
        """Contacts with these emails, then contacts with these names (first and last name
        equal, or the first name alone for one-word names). Returns them and what was searched."""
        rows: list[tuple[dict, str, str]] = []
        seen: set[str] = set()
        emails = list(dict.fromkeys(e.strip().lower() for e in emails if "@" in e))
        names = list(dict.fromkeys(n.strip() for n in names if n.strip()))
        async with self._client() as c:
            if emails:
                # IN takes lowercase values for string properties; up to 100 per filter.
                found = await self._search(
                    c,
                    [{"filters": [{"propertyName": "email", "operator": "IN", "values": emails}]}],
                    100,
                )
                by_email = {
                    str((r.get("properties") or {}).get("email", "")).lower(): r for r in found
                }
                for e in emails:
                    r = by_email.get(e)
                    if r and str(r["id"]) not in seen:
                        seen.add(str(r["id"]))
                        rows.append((r, "email", e))
            for i in range(0, len(names), SEARCH_GROUPS):
                batch = names[i : i + SEARCH_GROUPS]
                groups = []
                for n in batch:
                    first, _, last = n.partition(" ")
                    filters = [{"propertyName": "firstname", "operator": "EQ", "value": first}]
                    if last.strip():
                        filters.append(
                            {"propertyName": "lastname", "operator": "EQ", "value": last.strip()}
                        )
                    groups.append({"filters": filters})
                found = await self._search(c, groups, SEARCH_LIMIT * len(batch))
                for n in batch:
                    first, _, last = n.casefold().partition(" ")
                    hits = [
                        r
                        for r in found
                        if str((r.get("properties") or {}).get("firstname") or "").casefold()
                        == first
                        and (
                            not last.strip()
                            or str((r.get("properties") or {}).get("lastname") or "").casefold()
                            == last.strip()
                        )
                    ]
                    for r in hits[:SEARCH_LIMIT]:
                        if str(r["id"]) not in seen:
                            seen.add(str(r["id"]))
                            rows.append((r, "name", n))
            return await self._contacts(c, rows), [*emails, *names]

    async def _contacts_by_id(self, c, ids: list[str]) -> list[HubSpotContact]:
        body = await self._call(
            c,
            "POST",
            "/crm/v3/objects/contacts/batch/read",
            json={"properties": CONTACT_PROPERTIES, "inputs": [{"id": i} for i in ids]},
        )
        found = {str(r["id"]): r for r in body.get("results", [])}
        missing = [i for i in ids if i not in found]
        if missing:
            raise HubSpotError(f"HubSpot has no contact {', '.join(missing)}")
        return await self._contacts(c, [(found[i], "confirmed", "") for i in ids])

    # -- push

    async def _upsert(
        self,
        c: httpx.AsyncClient,
        kind: str,
        object_id: str | None,
        properties: dict,
        targets: list[tuple[str, str]],
        known: set[str],
    ) -> str:
        """PATCH the object when it exists (and associate it with targets it does not have
        yet), else create it with all the targets. An object deleted in HubSpot is recreated."""
        if object_id:
            try:
                await self._call(
                    c,
                    "PATCH",
                    f"/crm/v3/objects/{kind}/{object_id}",
                    json={"properties": properties},
                )
            except HubSpotError as e:
                if getattr(e, "status", None) != 404:
                    raise
                object_id = None
            else:
                for to_kind, to_id in targets:
                    if f"{to_kind}:{to_id}" in known:
                        continue
                    await self._call(
                        c,
                        "PUT",
                        f"/crm/v4/objects/{OBJECT_TYPE_IDS[kind]}/{object_id}/associations/default/"
                        f"{OBJECT_TYPE_IDS[to_kind]}/{to_id}",
                    )
                return object_id
        body = await self._call(
            c,
            "POST",
            f"/crm/v3/objects/{kind}",
            json={
                "properties": properties,
                "associations": [
                    {
                        "to": {"id": to_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": ASSOCIATION_TYPES[(kind, to_kind)],
                            }
                        ],
                    }
                    for to_kind, to_id in targets
                ],
            },
        )
        return str(body["id"])

    async def push(self, session: Session, contact_ids: list[str], state: HubSpotState) -> bool:
        """Write the meeting, note and tasks, updating `state` as each record is written (so a
        failure part way still remembers what exists). Returns True on a first push."""
        ids = list(dict.fromkeys(str(i) for i in contact_ids if str(i).strip()))
        if not ids:
            raise HubSpotError("Choose at least one HubSpot contact")
        if not session.summary:
            raise HubSpotError("Summarize the meeting first")
        first = state.meeting_id is None
        start, end = meeting_times(session)
        known = set(state.associated)
        async with self._client() as c:
            contacts = await self._contacts_by_id(c, ids)
            state.contacts = contacts
            owner = await self._owner(c)
            owner_prop = {"hubspot_owner_id": str(owner["id"])} if owner else {}
            targets = [("contacts", x.id) for x in contacts]
            for cid in dict.fromkeys(x.company_id for x in contacts if x.company_id):
                targets.append(("companies", cid))

            state.meeting_id = await self._upsert(
                c,
                "meetings",
                state.meeting_id,
                {
                    "hs_timestamp": _iso(start),
                    "hs_meeting_title": session.name,
                    "hs_meeting_body": markdown_html(session.summary),
                    "hs_meeting_start_time": _iso(start),
                    "hs_meeting_end_time": _iso(end),
                    **owner_prop,
                },
                targets,
                known,
            )
            if body := note_html(session):
                state.note_id = await self._upsert(
                    c,
                    "notes",
                    state.note_id,
                    {"hs_timestamp": _iso(start), "hs_note_body": body, **owner_prop},
                    targets,
                    known,
                )
            items = session.summary_data.action_items if session.summary_data else []
            for i, item in enumerate(items):
                existing = state.task_ids[i] if i < len(state.task_ids) else None
                task_id = await self._upsert(
                    c,
                    "tasks",
                    existing,
                    {
                        "hs_timestamp": _task_due(item, start),
                        "hs_task_subject": _subject(item),
                        "hs_task_body": _task_body(session, item, start),
                        "hs_task_status": "COMPLETED" if item.done else "NOT_STARTED",
                        "hs_task_type": "TODO",
                        **owner_prop,
                    },
                    targets,
                    # A task added since the last push has none of the associations yet.
                    known if existing else set(),
                )
                if i < len(state.task_ids):
                    state.task_ids[i] = task_id
                else:
                    state.task_ids.append(task_id)
        state.associated = sorted(known | {f"{k}:{v}" for k, v in targets})
        state.pushed_at = datetime.now()
        return first


def client_for(app: AppContext) -> HubSpotClient:
    st = app.settings
    return HubSpotClient(st.hubspot_token, st.hubspot_owner_email, app.http_transport)


def match_sources(app: AppContext, session: Session) -> tuple[list[str], list[str]]:
    """Emails (attendees the calendar gave an address for, or attendees written as one) and
    names (other attendees and the meeting's named speakers) to look up."""
    known = app.repo.attendee_emails(session.id)
    emails, names = [], []
    for a in session.attendees:
        if "@" in a:
            emails.append(a)
        elif known.get(a):
            emails.append(known[a])
        else:
            names.append(a)
    generic = (app.settings.local_speaker_name, app.settings.remote_speaker_name)
    for seg in session.transcript:
        if is_person(seg.speaker, generic) and seg.speaker not in names:
            names.append(seg.speaker)
    return emails, [n for n in names if is_person(n, generic)]


async def find_matches(app: AppContext, session: Session) -> HubSpotMatches:
    state = load_state(app, session.id)
    emails, names = match_sources(app, session)
    found, searched = await client_for(app).find_contacts(emails, names)
    confirmed = {x.id for x in state.contacts}
    candidates = [*state.contacts, *(x for x in found if x.id not in confirmed)]
    return HubSpotMatches(candidates=candidates, searched=searched, state=state)


def redacted(session: Session) -> Session:
    """What goes to HubSpot, with Social Security, account, routing and card numbers and dates
    of birth masked, as in exports (redact_exports): a CRM is outside the team server."""
    from ..summarization.privacy import redact_identifiers as r

    data = session.summary_data
    if data is not None:
        data = data.model_copy(
            update={
                "decisions": [r(x) for x in data.decisions],
                "open_questions": [r(x) for x in data.open_questions],
                "action_items": [
                    a.model_copy(update={"text": r(a.text)}) for a in data.action_items
                ],
                "client_facts": [
                    f.model_copy(update={"text": r(f.text)}) for f in data.client_facts
                ],
            }
        )
    return session.model_copy(update={"summary": r(session.summary), "summary_data": data})


async def push_session(
    app: AppContext, session: Session, contact_ids: list[str] | None = None, auto: bool = False
) -> HubSpotPushResult:
    """Push (or update) the meeting in HubSpot with these contacts (default: the confirmed
    ones), save what was written and log it in the meeting's history."""
    from . import history

    state = load_state(app, session.id)
    ids = contact_ids if contact_ids is not None else [x.id for x in state.contacts]
    outgoing = redacted(session) if app.settings.redact_exports else session
    try:
        created = await client_for(app).push(outgoing, ids, state)
    except Exception as e:
        history.log(app, session.id, "hubspot_failed", error=str(e), auto=auto)
        raise
    finally:
        if state.meeting_id or state.note_id or state.task_ids:
            save_state(app, session.id, state)
    tasks = len(session.summary_data.action_items) if session.summary_data else 0
    history.log(
        app,
        session.id,
        "hubspot_pushed",
        created=created,
        contacts=[x.name for x in state.contacts],
        tasks=tasks,
        auto=auto,
    )
    who = ", ".join(x.name for x in state.contacts)
    verb = "Sent to" if created else "Updated in"
    return HubSpotPushResult(
        created=created,
        message=f"{verb} HubSpot for {who}"
        + (f" with {tasks} task{'s' * (tasks != 1)}" if tasks else ""),
        state=state,
    )


async def auto_push(app: AppContext, session_id: str) -> str | None:
    """After a summary: update HubSpot when auto push is on and the meeting's contacts were
    confirmed before. Never raises; returns what happened, for the job result."""
    st = app.settings
    if not (st.hubspot_auto_push and st.hubspot_token):
        return None
    if not load_state(app, session_id).contacts:
        return None
    session = app.sessions.get_session(session_id)
    if session is None:
        return None
    try:
        return (await push_session(app, session, auto=True)).message
    except Exception as e:
        logger.warning("HubSpot auto push failed for session %s: %s", session_id, e)
        return f"HubSpot: {e}"
