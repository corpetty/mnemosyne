"""Per-person preferences on a team server (services/prefs.py, /api/users/me/prefs)."""

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import hubspot, prefs
from tests.conftest import run_summarize
from tests.fakes import FakeProvider


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name in ("Ann", "Bob"):
        user = ctx.users.add(name, f"{name.lower()}@team.test", "member")
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"})
    with TestClient(app) as client:
        yield client, ctx, people


def test_your_preferences_are_yours(team):
    client, ctx, people = team
    ann, bob = people["Ann"][1], people["Bob"][1]
    assert client.get("/api/users/me/prefs", headers=ann).json()["summary_style"] is None
    body = {"summary_style": "standup", "mention_keywords": "Ann, infra", "digest_weekday": 4}
    got = client.put("/api/users/me/prefs", json=body, headers=ann).json()
    assert got["summary_style"] == "standup" and got["digest_weekday"] == 4
    assert client.get("/api/users/me/prefs", headers=bob).json()["summary_style"] is None
    bad = client.put("/api/users/me/prefs", json={"digest_weekday": 9}, headers=ann)
    assert bad.status_code == 422


def test_the_desktop_app_has_settings_not_preferences(client):
    assert client.get("/api/users/me/prefs").status_code == 400


def test_effective_settings_put_preferences_and_your_name_on_top(team):
    _, ctx, people = team
    ann = people["Ann"][0]
    prefs.save(ctx, ann.id, prefs.UserPrefs(summary_style="client", mention_keywords="Ann"))
    mine = prefs.effective(ctx, ann.id)
    assert mine.summary_style == "client" and mine.mention_keywords == "Ann"
    assert mine.local_speaker_name == "Ann"  # the mic in Ann's recordings is Ann
    assert ctx.settings.summary_style == "meeting"  # the server's own are untouched
    assert prefs.effective(ctx, "").summary_style == "meeting"
    ctx.settings.team_mode = False
    assert prefs.effective(ctx, ann.id) is ctx.settings


def test_a_meeting_is_summarized_in_its_owners_style(team):
    client, ctx, people = team
    provider = FakeProvider()
    ctx.summarizer.providers = {"fake": provider}
    ctx.settings.default_provider = "fake"
    client.headers.update(people["Ann"][1])
    client.put("/api/users/me/prefs", json={"summary_style": "standup"})
    sid = client.post("/api/sessions", json={"name": "Daily"}).json()["id"]
    reset = access.current.set(access.Principal(people["Ann"][0].id, "Ann", "member"))
    try:
        ctx.sessions.set_transcript(
            sid, [TranscriptSegment(text="I did the thing.", speaker="Ann", start=0, end=2)]
        )
    finally:
        access.current.reset(reset)
    job = run_summarize(client, sid, {"provider": "fake"})
    assert job["status"] == "completed", job
    assert "daily standup" in provider.calls[-1]["system_prompt"]


def test_meetings_are_shared_with_everyone_when_you_say_so(team):
    client, ctx, people = team
    ann, bob = people["Ann"][1], people["Bob"][1]
    client.put("/api/users/me/prefs", json={"share_new_meetings": "team"}, headers=ann)
    sid = client.post("/api/sessions", json={"name": "Open"}, headers=ann).json()["id"]
    assert client.get(f"/api/sessions/{sid}", headers=bob).status_code == 200
    mine = client.post("/api/sessions", json={"name": "Bob's"}, headers=bob).json()["id"]
    assert client.get(f"/api/sessions/{mine}", headers=ann).status_code == 404


def test_calendar_and_hubspot_owner_follow_the_person(team):
    _, ctx, people = team
    ann, bob = people["Ann"][0], people["Bob"][0]
    ctx.settings.hubspot_token = "pat"
    ctx.settings.hubspot_owner_email = "server@team.test"
    prefs.save(
        ctx,
        ann.id,
        prefs.UserPrefs(calendar_ics_url="https://cal/ann.ics", hubspot_owner_email="ann@x.test"),
    )
    assert ctx.calendar_for(ann.id).source == "https://cal/ann.ics"
    assert ctx.calendar_for(ann.id) is ctx.calendar_for(ann.id)  # one per feed
    assert ctx.calendar_for(bob.id) is ctx.calendar
    reset = access.current.set(access.Principal(ann.id, "Ann", "member"))
    try:
        assert hubspot.client_for(ctx).owner_email == "ann@x.test"
    finally:
        access.current.reset(reset)
    assert hubspot.client_for(ctx, bob.id).owner_email == "server@team.test"
