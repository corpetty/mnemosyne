"""Your tasks and your digest on a team server (routes/tasks.py `mine`, AppContext digests)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import prefs, sharing
from mnemosyne.services.tasks import mine_matcher


def test_mine_matches_full_names_emails_and_unique_first_names():
    mine = mine_matcher(["Ann Lee", "ann@team.test"], ["Bob Stone", "Cat"])
    assert mine("Ann Lee") and mine("ann") and mine("ANN@team.test")
    assert not mine("Bob") and not mine("") and not mine(None)
    shared_first = mine_matcher(["Ann Lee"], ["Ann Ortiz"])
    assert shared_first("Ann Lee") and not shared_first("Ann")  # which Ann? not claimed


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name in ("Ann Lee", "Bob Stone"):
        user = ctx.users.add(name, f"{name.split()[0].lower()}@team.test", "member")
        code, _ = ctx.users.invite(user.id)
        people[name.split()[0]] = (
            user,
            {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"},
        )
    with TestClient(app) as client:
        yield client, ctx, people


def _meeting(ctx, user, name, owners, when=None):
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        s = Session(
            name=name,
            summary="Talked.",
            summary_data=SummaryData(
                action_items=[ActionItem(text=f"Task for {o}", owner=o) for o in owners]
            ),
        )
        if when:
            s.created_at = when
        return ctx.repo.save(s)
    finally:
        access.current.reset(reset)


def test_your_tasks_across_your_meetings_and_shared_ones(team):
    client, ctx, people = team
    ann, bob = people["Ann"], people["Bob"]
    _meeting(ctx, ann[0], "Ann's", ["Ann", "Bob"])
    theirs = _meeting(ctx, bob[0], "Bob's", ["ann@team.test", "Bob Stone"])
    got = client.get("/api/action-items", params={"mine": True}, headers=ann[1]).json()
    assert [t["text"] for t in got] == ["Task for Ann"]  # Bob's meeting is not shared yet
    sharing.set_shares(ctx, theirs.id, {ann[0].id})
    got = client.get("/api/action-items", params={"mine": True}, headers=ann[1]).json()
    assert sorted(t["text"] for t in got) == ["Task for Ann", "Task for ann@team.test"]
    every = client.get("/api/action-items", headers=ann[1]).json()
    assert {t["text"]: t["mine"] for t in every} == {
        "Task for Ann": True,
        "Task for Bob": False,
        "Task for ann@team.test": True,
        "Task for Bob Stone": False,
    }


def test_the_desktop_app_knows_its_tasks_by_the_mic_name(client, ctx):
    ctx.settings.local_speaker_name = "Corey"
    ctx.repo.save(
        Session(
            name="m",
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(text="a", owner="Corey"), ActionItem(text="b")]
            ),
        )
    )
    got = client.get("/api/action-items", params={"mine": True}).json()
    assert [t["text"] for t in got] == ["a"]


def test_each_person_gets_their_own_weekly_digest(team, monkeypatch):
    _, ctx, people = team
    ann, bob = people["Ann"][0], people["Bob"][0]
    friday = datetime(2026, 9, 25, 18)
    _meeting(ctx, ann, "Ann's week", ["Ann"], when=datetime(2026, 9, 23, 10))
    submitted = []

    def submit(kind, runner, **_):
        submitted.append((kind, access.user_id()))
        return kind

    monkeypatch.setattr(ctx.jobs, "submit", submit)
    assert ctx.maybe_schedule_digest(friday) == []  # nobody's schedule is on
    prefs.save(ctx, ann.id, prefs.UserPrefs(digest_weekday=4, digest_hour=17))
    prefs.save(ctx, bob.id, prefs.UserPrefs(digest_weekday=4, digest_hour=17))
    ctx.maybe_schedule_digest(friday)
    # Bob has no summarized meeting he can see that week: no digest for him.
    assert submitted == [("digest", ann.id)]
