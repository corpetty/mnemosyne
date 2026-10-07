"""Sharing a meeting on a team server (services/sharing.py, routes/sharing.py)."""

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import sharing


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [("Admin", "admin"), ("Ann", "member"), ("Bob", "member"), ("Cat", "member")]:
        user = ctx.users.add(name, f"{name.lower()}@team.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name] = (user, {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"})
    with TestClient(app) as client:
        yield client, ctx, people


def _meeting(ctx, people, owner, name):
    user = people[owner][0]
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        session = Session(
            name=name,
            transcript=[
                TranscriptSegment(text="the quarterly plan", speaker="Ann", start=0, end=2)
            ],
            summary="We planned.",
            summary_data=SummaryData(action_items=[ActionItem(text="Send the plan", owner="Bob")]),
        )
        return ctx.repo.save(session)
    finally:
        access.current.reset(reset)


def test_shared_with_one_person_they_read_it_and_change_nothing(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Planning")
    ann, bob, cat = people["Ann"][1], people["Bob"][1], people["Cat"][1]
    assert client.get(f"/api/sessions/{s.id}", headers=bob).status_code == 404

    r = client.put(
        f"/api/sessions/{s.id}/shares", json={"user_ids": [people["Bob"][0].id]}, headers=ann
    )
    assert r.status_code == 200 and [p["name"] for p in r.json()["people"]] == ["Bob"]
    assert client.get(f"/api/sessions/{s.id}", headers=bob).status_code == 200
    assert s.id in [m["id"] for m in client.get("/api/sessions", headers=bob).json()]
    assert client.get(f"/api/sessions/{s.id}", headers=cat).status_code == 404
    # Read only: no renaming, no resharing.
    assert client.patch(f"/api/sessions/{s.id}", json={"name": "x"}, headers=bob).status_code == 403
    r = client.put(f"/api/sessions/{s.id}/shares", json={"team": True}, headers=bob)
    assert r.status_code == 403
    assert client.get(f"/api/sessions/{s.id}/shares", headers=bob).json()["can_change"] is False
    # What they see across meetings follows: search, tasks.
    hits = client.get("/api/search", params={"q": "quarterly"}, headers=bob).json()
    assert s.id in {h["session_id"] for h in hits}
    tasks = client.get("/api/action-items", params={"status": "all"}, headers=bob).json()
    assert [t["text"] for t in tasks] == ["Send the plan"]

    kinds = [e["kind"] for e in ctx.repo.events(s.id)]
    assert "shared" in kinds
    client.put(f"/api/sessions/{s.id}/shares", json={"user_ids": []}, headers=ann)
    assert client.get(f"/api/sessions/{s.id}", headers=bob).status_code == 404
    assert "unshared" in [e["kind"] for e in ctx.repo.events(s.id)]


def test_shared_with_everyone(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "All hands")
    r = client.put(f"/api/sessions/{s.id}/shares", json={"team": True}, headers=people["Admin"][1])
    assert r.status_code == 200 and r.json()["team"] is True
    for name in ("Bob", "Cat"):
        assert client.get(f"/api/sessions/{s.id}", headers=people[name][1]).status_code == 200


def test_meetings_are_shared_with_invited_team_members(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Review")
    ctx.repo.set_attendee_emails(s.id, {"Bob": "BOB@team.test", "Zed": "zed@elsewhere.test"})
    sharing.share_with_invitees(ctx, s.id)
    assert {x["user_id"] for x in ctx.repo.shares(s.id)} == {people["Bob"][0].id}
    ctx.settings.share_with_invitees = False
    t = _meeting(ctx, people, "Ann", "Private")
    ctx.repo.set_attendee_emails(t.id, {"Cat": "cat@team.test"})
    sharing.share_with_invitees(ctx, t.id)
    assert ctx.repo.shares(t.id) == []


def test_the_event_stream_follows_sharing(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Live")
    sharing.set_shares(ctx, s.id, {people["Bob"][0].id})
    token = people["Bob"][1]["Authorization"].split()[1]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "hello"
        ctx.bus.publish({"type": "live_segment", "session_id": s.id, "segment": {"text": "hi"}})
        assert ws.receive_json()["segment"]["text"] == "hi"


def test_a_reader_cannot_stop_the_owners_work(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Busy")
    sharing.set_shares(ctx, s.id, {people["Bob"][0].id})
    from mnemosyne.jobs import Job

    job = Job(kind="transcribe", session_id=s.id, owner_id=people["Ann"][0].id)
    ctx.jobs.jobs[job.id] = job
    r = client.post(f"/api/jobs/{job.id}/cancel", headers=people["Bob"][1])
    assert r.status_code == 403


def test_a_reader_ticks_off_their_action_item(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Plan")
    bob = people["Bob"][1]
    r = client.patch(f"/api/sessions/{s.id}/action-items/0", json={"done": True}, headers=bob)
    assert r.status_code == 404  # not shared with Bob yet
    sharing.set_shares(ctx, s.id, {people["Bob"][0].id})
    r = client.patch(f"/api/sessions/{s.id}/action-items/0", json={"done": True}, headers=bob)
    assert r.status_code == 200 and r.json()["done"] is True
    assert ctx.repo.get(s.id).summary_data.action_items[0].done
    done = [e for e in ctx.repo.events(s.id) if e["kind"] == "task_done"]
    assert done and done[0]["detail"]["by"] == "Bob"


def test_a_reader_ticks_but_does_not_edit(team):
    client, ctx, people = team
    s = _meeting(ctx, people, "Ann", "Plan")
    bob = people["Bob"][1]
    sharing.set_shares(ctx, s.id, {people["Bob"][0].id})
    tasks = client.get("/api/action-items", params={"status": "all"}, headers=bob).json()
    assert [t["can_edit"] for t in tasks] == [False]
    url = f"/api/sessions/{s.id}/action-items/0"
    assert client.patch(url, json={"text": "Send it later"}, headers=bob).status_code == 403
    assert ctx.repo.get(s.id).summary_data.action_items[0].text == "Send the plan"
    ann = people["Ann"][1]
    assert client.get("/api/action-items", params={"status": "all"}, headers=ann).json()[0][
        "can_edit"
    ]


def test_a_member_renames_topics_in_their_own_meetings_only(team):
    client, ctx, people = team
    ann_meeting = _meeting(ctx, people, "Ann", "Plan")
    bob_meeting = _meeting(ctx, people, "Bob", "Also a plan")
    for s in (ann_meeting, bob_meeting):
        data = ctx.repo.get(s.id).summary_data.model_copy(update={"topics": ["Waku"]})
        with ctx.repo._lock, ctx.repo._conn:
            ctx.repo._conn.execute(
                "UPDATE sessions SET summary_data=? WHERE id=?", (data.model_dump_json(), s.id)
            )
    sharing.set_shares(ctx, ann_meeting.id, {people["Bob"][0].id})  # Bob reads Ann's
    bob = people["Bob"][1]
    r = client.post("/api/topics/rename", json={"old": "waku", "new": "Messaging"}, headers=bob)
    assert r.json() == {"meetings": 1, "remembered": False}  # not a team-wide alias
    assert ctx.repo.get(bob_meeting.id).summary_data.topics == ["Messaging"]
    assert ctx.repo.get(ann_meeting.id).summary_data.topics == ["Waku"]
    assert ctx.settings.topic_aliases == {}
