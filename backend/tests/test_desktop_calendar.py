"""Calendars from GNOME Online Accounts / evolution-data-server, over a fake D-Bus."""

from datetime import datetime, timedelta

import pytest

from mnemosyne.config import Settings
from mnemosyne.services import desktop_calendar as dc
from mnemosyne.services.calendar_service import CalendarService, calendar_source, parse_events

EDS = "org.gnome.evolution.dataserver"


def source(uid, name, parent="", calendar=True, backend="google", enabled=True):
    data = f"[Data Source]\nDisplayName={name}\nEnabled={'true' if enabled else 'false'}\n"
    data += f"Parent={parent}\n"
    if calendar:
        data += f"\n[Calendar]\nBackendName={backend}\n"
    return {f"{EDS}.Source": {"UID": ("s", uid), "Data": ("s", data)}}


EVENT = (
    "BEGIN:VEVENT\r\nUID:standup-1\r\nSUMMARY:Daily standup\r\n"
    "DTSTART;TZID=America/Chicago:{start}\r\nDTEND;TZID=America/Chicago:{end}\r\n"
    "ATTENDEE;CN=Ana Silva:mailto:ana@example.com\r\nEND:VEVENT"
)


class FakeBus:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.objects = {
            "/s/1": source("work-account", "corey@company.com", calendar=False),
            "/s/2": source("work-cal", "Corey (work)", parent="work-account"),
            "/s/3": source("personal", "Personal", backend="local"),
            "/s/4": source("birthdays", "Birthdays", backend="contacts"),
            "/s/5": source("old", "Old calendar", enabled=False),
        }

    def names(self):
        return [f"{EDS}.Sources5", f"{EDS}.Calendar8", "org.other"]

    def call(self, bus_name, path, interface, method, sig="", args=()):
        self.calls.append((method, args))
        if method == "GetManagedObjects":
            return (self.objects,)
        if method == "OpenCalendar":
            return (f"/cal/{args[0]}", f"{EDS}.Calendar8")
        if method == "GetObjectList":
            assert "occur-in-time-range?" in args[0]
            return (self.events.get(path, []),)
        if method == "GetTimezone":
            return ("BEGIN:VTIMEZONE\r\nTZID:America/Chicago\r\nEND:VTIMEZONE",)
        return ()

    def close(self):
        pass


def test_lists_meeting_calendars_with_their_account():
    cals = dc.list_calendars(FakeBus({}))
    assert [(c.uid, c.name, c.account) for c in cals] == [
        ("personal", "Personal", "On This Computer"),
        ("work-cal", "Corey (work)", "corey@company.com"),
    ]  # no birthdays, nothing disabled, the account itself is not a calendar


def test_reads_events_into_one_calendar_feed():
    now = datetime(2026, 9, 28, 9, 0).astimezone()
    bus = FakeBus({"/cal/work-cal": [EVENT.format(start="20260928T090000", end="20260928T091500")]})
    text = dc.read_events(["work-cal"], now - timedelta(days=1), now + timedelta(days=1), bus)
    assert text.startswith("BEGIN:VCALENDAR") and "VTIMEZONE" in text
    assert [m for m, _ in bus.calls].count("Close") == 1
    events = parse_events(text, now - timedelta(days=1), now + timedelta(days=1))
    assert [(e.title, e.attendees) for e in events] == [("Daily standup", ["Ana Silva"])]


def test_all_calendars_when_none_are_chosen():
    bus = FakeBus({})
    dc.read_events([], datetime(2026, 9, 28).astimezone(), datetime(2026, 9, 29).astimezone(), bus)
    opened = [args[0] for m, args in bus.calls if m == "OpenCalendar"]
    assert opened == ["personal", "work-cal"]


def test_calendar_source_setting():
    assert calendar_source(Settings(calendar_source="off", calendar_ics_url="https://x")) == ""
    assert calendar_source(Settings(calendar_source="ics", calendar_ics_url="https://x")) == (
        "https://x"
    )
    assert calendar_source(
        Settings(calendar_source="desktop", calendar_desktop_calendars="a, b")
    ) == ("desktop:a,b")


@pytest.mark.anyio
async def test_the_calendar_service_reads_desktop_calendars(monkeypatch):
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    fmt = "%Y%m%dT%H%M%S"
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        + EVENT.format(
            start=(now - timedelta(minutes=5)).astimezone(dc_tz()).strftime(fmt),
            end=(now + timedelta(minutes=25)).astimezone(dc_tz()).strftime(fmt),
        )
        + "\r\nEND:VCALENDAR\r\n"
    )
    seen = []
    monkeypatch.setattr(dc, "read_events", lambda uids, s, e: seen.append(uids) or text)
    cal = CalendarService("desktop:work-cal")
    assert cal.configured
    current = await cal.current()
    assert current is not None and current.title == "Daily standup"
    assert seen == [["work-cal"]]


def dc_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/Chicago")
