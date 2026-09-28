"""An agenda the copilot follows (services/copilot.py, calendar descriptions, PUT agenda)."""

import json

import pytest

from mnemosyne.export.obsidian import ObsidianExporter
from mnemosyne.models.session import AgendaItem, CopilotNotes
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services.calendar_service import agenda_from_description, plain_text
from mnemosyne.services.copilot import agenda_hint, update_notes


def test_agenda_points_come_from_the_event_description():
    html = (
        "<p>Hi all,</p><p><b>Agenda</b></p><ol><li>Beta date</li><li>Pricing &amp; tiers</li>"
        "</ol><p>Doc: https://docs.example.com/x</p>"
    )
    text = plain_text(html)
    assert "Pricing & tiers" in text and "<" not in text
    assert agenda_from_description(text) == ["Beta date", "Pricing & tiers"]
    plain = "Notes first\n\nAgenda:\n1. Intros\n2) Roadmap\n\nThanks!\n- not agenda"
    assert agenda_from_description(plain) == ["Intros", "Roadmap"]
    assert agenda_from_description("* one\n• two\n- three") == ["one", "two", "three"]
    assert agenda_from_description("Just a note, no list.") == []


@pytest.mark.anyio
async def test_the_copilot_marks_agenda_points_covered():
    lines = [TranscriptSegment(text=f"line {i}", speaker="A", start=i, end=i + 1) for i in range(3)]
    seen = {}

    async def complete(system, user):
        seen["system"], seen["user"] = system, user
        return json.dumps({"summary": ["x"], "agenda_covered": [2, 9, "1"]})

    previous = CopilotNotes(session_id="s", agenda_covered=[1], lines=1)
    notes = await update_notes(complete, previous, "s", lines, ["Intros", "Roadmap", "AOB"])
    assert notes.agenda_covered == [1, 2]  # 9 is out of range, "1" is not a number; 1 stays
    assert "agenda_covered" in seen["system"] and "1. Intros\n2. Roadmap" in seen["user"]

    async def no_agenda(system, user):
        seen["system"] = system
        return json.dumps({"summary": ["x"]})

    await update_notes(no_agenda, None, "s", lines)
    assert "agenda" not in seen["system"]


def test_agenda_is_set_kept_and_exported(client, ctx, tmp_path):
    s = ctx.sessions.create_session("Planning")
    items = [
        {"text": " Beta date ", "covered": False},
        {"text": "Pricing", "covered": True},
        {"text": " "},
    ]
    got = client.put(f"/api/sessions/{s.id}/agenda", json={"items": items}).json()
    assert got == [{"text": "Beta date", "covered": False}, {"text": "Pricing", "covered": True}]
    session = ctx.sessions.get_session(s.id)
    assert [a.text for a in session.agenda] == ["Beta date", "Pricing"]
    assert "2. Pricing" in agenda_hint(session.agenda) and "Not discussed" in agenda_hint(
        session.agenda
    )
    note = ObsidianExporter(str(tmp_path)).render(session)
    assert "## Agenda\n\n- [ ] Beta date\n- [x] Pricing" in note
    assert client.put("/api/sessions/nope/agenda", json={"items": []}).status_code == 404
    assert agenda_hint([]) == "" and AgendaItem(text="x").covered is False


def test_recording_during_an_event_takes_its_agenda(client, ctx, fake_pipewire, tmp_path):
    from tests.test_calendar import FixedCalendar, at

    ics = tmp_path / "cal.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nUID:p-1\r\n"
        "SUMMARY:Planning\r\nDTSTART:20260923T100000Z\r\nDTEND:20260923T110000Z\r\n"
        "DESCRIPTION:Agenda\\n- Beta date\\n- Pricing\\n\\nDoc https://docs.example.com/d/1"
        "\\nJoin https://meet.google.com/abc\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    ctx.settings.live_transcription = False
    ctx.calendar = FixedCalendar(str(ics), at(10, 2))
    sid = client.post("/api/audio/start", json={"device_ids": [1]}).json()["session_id"]
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["name"] == "Planning"
    assert [a["text"] for a in session["agenda"]] == ["Beta date", "Pricing"]
    assert [a["url"] for a in session["assets"]] == ["https://docs.example.com/d/1"]  # not the call
    client.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
