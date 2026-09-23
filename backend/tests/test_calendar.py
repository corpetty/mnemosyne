"""Calendar (ICS) parsing, current-meeting selection, and naming at recording start."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from src.mnemosyne.services.calendar_service import CalendarService, parse_events

UTC = UTC

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup-1
SUMMARY:Daily standup
DTSTART:20260921T090000Z
DTEND:20260921T091500Z
RRULE:FREQ=DAILY;COUNT=10
ORGANIZER;CN=Corey Petty:mailto:corey@example.com
ATTENDEE;CN=Alice Smith:mailto:alice@example.com
ATTENDEE:mailto:bob.jones@example.com
ATTENDEE;CUTYPE=ROOM;CN=Room 4:mailto:room4@example.com
END:VEVENT
BEGIN:VEVENT
UID:release-1
SUMMARY:Release sync
DTSTART:20260923T100000Z
DTEND:20260923T110000Z
LOCATION:https://meet.example.com/abc
ATTENDEE;CN=Alice Smith:mailto:alice@example.com
END:VEVENT
BEGIN:VEVENT
UID:overlap-1
SUMMARY:Quick sidebar
DTSTART:20260923T103000Z
DTEND:20260923T104500Z
END:VEVENT
BEGIN:VEVENT
UID:allday-1
SUMMARY:Company holiday
DTSTART;VALUE=DATE:20260923
DTEND;VALUE=DATE:20260924
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1
SUMMARY:Cancelled thing
DTSTART:20260923T120000Z
DTEND:20260923T130000Z
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:block-1
SUMMARY:Focus day
DTSTART:20260923T000000Z
DTEND:20260923T230000Z
END:VEVENT
END:VCALENDAR
"""


def at(h, m=0, day=23):
    return datetime(2026, 9, day, h, m, tzinfo=UTC)


def test_parse_expands_recurrence_and_filters():
    evs = parse_events(ICS, at(0), at(23, 59))
    titles = [e.title for e in evs]
    assert titles == ["Daily standup", "Release sync", "Quick sidebar"]
    standup = evs[0]
    assert standup.start == at(9) and standup.end == at(9, 15)
    # organizer first, CN preferred, email fallback prettified, rooms skipped
    assert standup.attendees == ["Corey Petty", "Alice Smith", "Bob Jones"]
    assert evs[1].location.startswith("https://")
    # recurrence: one standup per day
    week = parse_events(ICS, at(0, day=21), at(23, day=27))
    assert sum(e.title == "Daily standup" for e in week) == 7


@pytest.fixture
def ics_file(tmp_path):
    f = tmp_path / "cal.ics"
    f.write_text(ICS)
    return f


@pytest.mark.anyio
async def test_current_prefers_latest_started_then_soonest(ics_file):
    cal = CalendarService(str(ics_file))
    assert (await cal.current(at(10, 40))).title == "Quick sidebar"  # nested meeting wins
    assert (await cal.current(at(10, 50))).title == "Release sync"
    assert (await cal.current(at(9, 55))).title == "Release sync"  # starts in 5 min
    assert await cal.current(at(9, 40)) is None  # nothing within 10 min
    assert (await cal.current(at(9, 5))).title == "Daily standup"
    upcoming = await cal.upcoming(hours=2, at=at(9, 50))
    assert [e.title for e in upcoming] == ["Release sync", "Quick sidebar"]


@pytest.mark.anyio
async def test_http_fetch_caches_and_survives_errors():
    calls = {"n": 0, "fail": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.scheme == "https"  # webcal:// rewritten
        if calls["fail"]:
            return httpx.Response(500)
        return httpx.Response(200, text=ICS)

    cal = CalendarService(
        "webcal://cal.example.com/secret.ics", transport=httpx.MockTransport(handler)
    )
    assert (await cal.current(at(10, 50))).title == "Release sync"
    await cal.current(at(10, 50))
    assert calls["n"] == 1  # cached
    calls["fail"] = True
    evs = await cal.events(at(0), at(23), force=True)
    assert calls["n"] == 2 and len(evs) == 3  # last good copy still served
    assert "HTTPStatusError" in cal.last_error


@pytest.mark.anyio
async def test_bad_source_is_empty_not_an_error(tmp_path):
    missing = CalendarService(str(tmp_path / "nope.ics"))
    assert await missing.current() is None and missing.last_error
    notcal = tmp_path / "x.ics"
    notcal.write_text("hello")
    assert await CalendarService(str(notcal)).upcoming() == []
    assert CalendarService("").configured is False


class FixedCalendar(CalendarService):
    """CalendarService pinned to a moment in time."""

    def __init__(self, source, now):
        super().__init__(source)
        self.now = now

    async def current(self, at=None, lookahead_minutes=10):
        return await super().current(self.now, lookahead_minutes)


def test_recording_start_names_session_from_calendar(client, ctx, fake_pipewire, ics_file):
    ctx.settings.live_transcription = False
    ctx.calendar = FixedCalendar(str(ics_file), at(9, 3))
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["name"] == "Daily standup"
    assert session["attendees"] == ["Corey Petty", "Alice Smith", "Bob Jones"]
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})

    # Custom names are left alone; no meeting means no change.
    named = client.post("/api/sessions", json={"name": "My call"}).json()["id"]
    client.post("/api/audio/start", json={"device_ids": [1], "session_id": named})
    assert client.get(f"/api/sessions/{named}").json()["name"] == "My call"
    client.post(f"/api/audio/stop/{named}", json={"transcribe": False})

    ctx.calendar = FixedCalendar(str(ics_file), at(15))
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    assert client.get(f"/api/sessions/{sid}").json()["name"] == "Untitled Session"
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})


def test_calendar_endpoint_and_attendees(client, ctx, ics_file):
    assert client.get("/api/calendar").json() == {
        "configured": False,
        "error": None,
        "current": None,
        "upcoming": [],
    }
    ctx.calendar = CalendarService(str(ics_file))
    body = client.get("/api/calendar", params={"hours": 24}).json()
    assert body["configured"] and body["error"] is None

    sid = client.post("/api/sessions", json={}).json()["id"]
    resp = client.put(
        f"/api/sessions/{sid}/attendees", json={"attendees": [" Ann ", "Ann", "", "Bo"]}
    )
    assert resp.json()["attendees"] == ["Ann", "Bo"]
    assert client.put("/api/sessions/nope/attendees", json={"attendees": []}).status_code == 404


def test_calendar_url_is_secret_and_rebuilds(client, ctx, ics_file):
    client.put("/api/settings", json={"calendar_ics_url": str(ics_file)})
    body = client.get("/api/settings").json()
    assert body["values"]["calendar_ics_url"] == "" and body["secrets_set"]["calendar_ics_url"]
    assert ctx.calendar.source == str(ics_file)


def test_attendees_reach_summary_prompt_and_export(client, ctx, fake_provider, tmp_path):
    from src.mnemosyne.export.obsidian import ObsidianExporter
    from src.mnemosyne.models.transcript import TranscriptSegment

    from tests.conftest import run_summarize

    sid = client.post("/api/sessions", json={"name": "Release sync"}).json()["id"]
    ctx.sessions.set_transcript(sid, [TranscriptSegment(text="hi", speaker="S", start=0, end=1)])
    client.put(f"/api/sessions/{sid}/attendees", json={"attendees": ["Alice Smith", "Bob"]})
    run_summarize(client, sid, {"provider": "fake"})
    prompt = fake_provider.calls[-1]["system_prompt"]
    assert 'Scheduled attendees of "Release sync": Alice Smith, Bob' in prompt
    md = ObsidianExporter(str(tmp_path)).render(ctx.sessions.get_session(sid))
    assert 'attendees: ["[[Alice Smith]]", "[[Bob]]"]' in md


def test_storage_roundtrip_keeps_attendees(ctx):
    s = ctx.sessions.create_session("x")
    ctx.repo.update_fields(s.id, attendees=["A", "B"])
    assert ctx.repo.get(s.id).attendees == ["A", "B"]
    assert datetime.now() - ctx.repo.get(s.id).updated_at < timedelta(minutes=1)


def test_new_session_named_during_meeting(client, ctx, ics_file):
    ctx.calendar = FixedCalendar(str(ics_file), at(10, 50))
    body = client.post("/api/sessions", json={}).json()
    assert body["name"] == "Release sync" and body["attendees"] == ["Alice Smith"]
    assert client.post("/api/sessions", json={"name": "Mine"}).json()["name"] == "Mine"
    ctx.settings.calendar_auto_name = False
    assert client.post("/api/sessions", json={}).json()["name"] == "Untitled Session"
