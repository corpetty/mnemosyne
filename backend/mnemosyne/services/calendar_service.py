"""Calendar awareness from an iCalendar (ICS) feed.

Works with any provider's private/secret ICS address (Google, Fastmail, Proton,
Outlook, Nextcloud) or a local .ics file, so there is no OAuth in the app.
Recurring events are expanded with recurring-ical-events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import Field

from ..models.base import ApiModel

logger = logging.getLogger(__name__)

# Events longer than this are treated as blocks (OOO, "focus time"), not meetings.
MAX_MEETING = timedelta(hours=8)


class CalendarEvent(ApiModel):
    uid: str
    title: str
    start: datetime
    end: datetime
    location: str = ""
    attendees: list[str] = Field(default_factory=list)


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _aware(value) -> datetime | None:
    """ICS start/end to an aware datetime; all-day dates return None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_local_tz())
    if isinstance(value, date):
        return None
    return None


def _person(addr) -> str | None:
    """A display name for an ATTENDEE/ORGANIZER, skipping rooms and resources."""
    params = getattr(addr, "params", {}) or {}
    if str(params.get("CUTYPE", "INDIVIDUAL")).upper() in ("ROOM", "RESOURCE"):
        return None
    name = str(params.get("CN", "")).strip().strip('"')
    email = str(addr).removeprefix("mailto:").removeprefix("MAILTO:").strip()
    if name and "@" not in name:
        return name
    if email and "@" in email:
        local = email.split("@", 1)[0]
        return " ".join(part.capitalize() for part in local.replace("_", ".").split(".") if part)
    return name or None


def parse_events(ics_text: str, start: datetime, end: datetime) -> list[CalendarEvent]:
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics_text)
    events = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s = _aware(comp.decoded("DTSTART", None))
        if s is None:
            continue  # all-day
        e = _aware(comp.decoded("DTEND", None)) if comp.get("DTEND") else None
        if e is None:
            dur = comp.decoded("DURATION", None)
            e = s + (dur if isinstance(dur, timedelta) else timedelta(hours=1))
        if e - s > MAX_MEETING:
            continue
        people: list[str] = []
        raw = comp.get("ATTENDEE") or []
        for addr in [comp.get("ORGANIZER"), *(raw if isinstance(raw, list) else [raw])]:
            if addr is None:
                continue
            name = _person(addr)
            if name and name not in people:
                people.append(name)
        events.append(
            CalendarEvent(
                uid=f"{comp.get('UID', '')}@{s.isoformat()}",
                title=str(comp.get("SUMMARY", "")).strip() or "Untitled event",
                start=s,
                end=e,
                location=str(comp.get("LOCATION", "")).strip(),
                attendees=people,
            )
        )
    events.sort(key=lambda ev: ev.start)
    return events


class CalendarService:
    def __init__(
        self,
        source: str,
        refresh_seconds: float = 600,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.source = source.strip()
        self.refresh_seconds = refresh_seconds
        self._transport = transport
        self._text: str | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.source)

    async def _load(self, force: bool = False) -> str | None:
        if not self.configured:
            return None
        async with self._lock:
            fresh = time.monotonic() - self._fetched_at < self.refresh_seconds
            if self._text is not None and fresh and not force:
                return self._text
            try:
                src = self.source
                if src.startswith("webcal://"):
                    src = "https://" + src[len("webcal://") :]
                if src.startswith(("http://", "https://")):
                    async with httpx.AsyncClient(
                        timeout=15, follow_redirects=True, transport=self._transport
                    ) as client:
                        resp = await client.get(src)
                        resp.raise_for_status()
                        text = resp.text
                else:
                    text = Path(src).expanduser().read_text(encoding="utf-8")
                if "BEGIN:VCALENDAR" not in text:
                    raise ValueError("Not an iCalendar feed (no BEGIN:VCALENDAR)")
                self._text, self._fetched_at, self.last_error = text, time.monotonic(), None
            except Exception as e:
                # Keep serving the last good copy if there is one.
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning("Calendar fetch failed: %s", self.last_error)
                if self._text is None:
                    return None
            return self._text

    async def events(self, start: datetime, end: datetime, force: bool = False):
        text = await self._load(force)
        if text is None:
            return []
        try:
            return parse_events(text, start, end)
        except Exception as e:
            self.last_error = f"Could not parse calendar: {e}"
            logger.warning(self.last_error)
            return []

    async def current(self, at: datetime | None = None, lookahead_minutes: int = 10):
        """The meeting in progress (latest-started wins), else the next one starting
        within `lookahead_minutes`."""
        at = (at or datetime.now()).astimezone()
        evs = await self.events(at - MAX_MEETING, at + timedelta(minutes=lookahead_minutes))
        running = [e for e in evs if e.start <= at < e.end]
        if running:
            return max(running, key=lambda e: e.start)
        soon = [e for e in evs if at < e.start <= at + timedelta(minutes=lookahead_minutes)]
        return min(soon, key=lambda e: e.start) if soon else None

    async def upcoming(self, hours: float = 12, at: datetime | None = None):
        at = (at or datetime.now()).astimezone()
        evs = await self.events(at - MAX_MEETING, at + timedelta(hours=hours))
        return [e for e in evs if e.end > at]
