"""Comments on tasks and decisions across meetings (services/comments.py, routes/tasks.py)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import decision_links, sharing, task_links

TID = "aaaaaaaaaaaa"
DID = "bbbbbbbbbbbb"


def _meeting(ctx, name, day, item_id, decision_id):
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, day, 10),
            summary="s",
            summary_data=SummaryData(
                action_items=[ActionItem(id=item_id, text="Update the docs", task_id=TID)],
                decisions=["We ship"],
                decision_ids=[decision_id],
                decision_links={decision_id: DID},
            ),
        )
    )


def _pair(ctx):
    old = _meeting(ctx, "Old", 1, "a1", "d1")
    new = _meeting(ctx, "New", 8, "b1", "e1")
    return old, new


def _post(client, kind, key, text="Note", headers=None, **kw):
    return client.post(
        f"/api/{kind}/{key}/comments", json={"text": text, **kw}, headers=headers or {}
    )


def test_threads_from_both_mentions_are_grouped(client, ctx):
    old, new = _pair(ctx)
    c1 = client.post(
        f"/api/sessions/{old.id}/comments",
        json={"text": "old thread", "target": "item", "target_id": "a1"},
    ).json()
    client.post(
        f"/api/sessions/{new.id}/comments",
        json={"text": "new thread", "target": "item", "target_id": "b1"},
    )
    # A thread on something else in the meeting is not the task's.
    client.post(f"/api/sessions/{old.id}/comments", json={"text": "whole meeting"})
    got = client.get(f"/api/tasks/{TID}/comments").json()["mentions"]
    assert [(g["session_id"], g["item_id"]) for g in got] == [(new.id, "b1"), (old.id, "a1")]
    assert [t["root"]["text"] for g in got for t in g["threads"]] == ["new thread", "old thread"]
    assert got[1]["threads"][0]["root"]["id"] == c1["id"]
    assert client.get("/api/tasks/nope/comments").status_code == 404


def test_posting_from_the_object_lands_on_the_head_mention(client, ctx):
    old, new = _pair(ctx)
    r = _post(client, "tasks", TID, "from the tasks view")
    assert r.status_code == 200
    c = r.json()
    assert (c["session_id"], c["target"], c["target_id"]) == (new.id, "item", "b1")
    # A reply goes to the meeting of its thread, even when that is not the head.
    older = client.post(
        f"/api/sessions/{old.id}/comments",
        json={"text": "root", "target": "item", "target_id": "a1"},
    ).json()
    rep = _post(client, "tasks", TID, "reply", parent_id=older["id"]).json()
    assert rep["session_id"] == old.id and rep["parent_id"] == older["id"]
    elsewhere = client.post(f"/api/sessions/{old.id}/comments", json={"text": "meeting"}).json()
    # A thread that is not on this task is not one to reply to from it... but its meeting is
    # a mention, so it is accepted; only unknown ids are refused.
    assert _post(client, "tasks", TID, "x", parent_id="nothere").status_code == 404
    assert elsewhere["id"]
    assert _post(client, "tasks", "nope").status_code == 404
    assert _post(client, "tasks", TID, "  ").status_code == 400


def test_decisions_likewise(client, ctx):
    old, new = _pair(ctx)
    c = _post(client, "decisions", DID, "why?").json()
    assert (c["session_id"], c["target"], c["target_id"]) == (new.id, "decision", "e1")
    got = client.get(f"/api/decisions/{DID}/comments").json()["mentions"]
    assert [g["session_id"] for g in got] == [new.id, old.id]
    assert [len(g["threads"]) for g in got] == [1, 0]


def test_open_comments_counts_open_roots_only(client, ctx):
    old, new = _pair(ctx)
    a = _post(client, "tasks", TID, "one").json()
    _post(client, "tasks", TID, "two")
    _post(client, "tasks", TID, "reply", parent_id=a["id"])  # replies do not count
    client.post(
        f"/api/sessions/{old.id}/comments",
        json={"text": "old", "target": "item", "target_id": "a1"},
    )
    assert task_links.index(ctx.repo)[TID].open_comments == 3
    assert client.get("/api/tasks").json()[0]["open_comments"] == 3
    assert decision_links.index(ctx.repo)[DID].open_comments == 0
    client.post(f"/api/sessions/{new.id}/comments/{a['id']}/resolve")
    assert task_links.index(ctx.repo)[TID].open_comments == 2
    d = _post(client, "decisions", DID, "d").json()
    assert decision_links.object_of(ctx.repo, new.id, "e1").open_comments == 1
    client.delete(f"/api/sessions/{new.id}/comments/{d['id']}")
    assert decision_links.index(ctx.repo)[DID].open_comments == 0


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


def _owned(ctx, user, *args):
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        return _meeting(ctx, *args)
    finally:
        access.current.reset(reset)


def test_a_mention_the_caller_cannot_see_is_left_out(team):
    client, ctx, people = team
    (ann, ah), (bob, bh) = people["Ann"], people["Bob"]
    mine = _owned(ctx, ann, "Ann's", 1, "a1", "d1")
    theirs = _owned(ctx, bob, "Bob's", 8, "b1", "e1")  # the newer: Bob's head
    for sid, item, h in ((theirs.id, "b1", bh), (mine.id, "a1", ah)):
        client.post(
            f"/api/sessions/{sid}/comments",
            json={"text": "t", "target": "item", "target_id": item},
            headers=h,
        )
        client.post(
            f"/api/sessions/{sid}/comments",
            json={"text": "d", "target": "decision", "target_id": "e1" if item == "b1" else "d1"},
            headers=h,
        )
    got = client.get(f"/api/tasks/{TID}/comments", headers=ah).json()["mentions"]
    assert [g["session_id"] for g in got] == [mine.id]
    assert client.get("/api/tasks", headers=ah).json()[0]["open_comments"] == 1
    assert client.get("/api/decisions", headers=ah).json()[0]["open_comments"] == 1
    # Posting from the object goes to the head she can see, not Bob's newer meeting.
    c = _post(client, "tasks", TID, "hi", headers=ah).json()
    assert c["session_id"] == mine.id
    # Bob cannot read Ann's meeting: only his own thread counts.
    assert client.get("/api/tasks", headers=bh).json()[0]["open_comments"] == 1
    # Shared, Ann reads Bob's meeting and its thread; posting now reaches the shared head.
    sharing.set_shares(ctx, theirs.id, {ann.id})
    got = client.get(f"/api/tasks/{TID}/comments", headers=ah).json()["mentions"]
    assert [g["session_id"] for g in got] == [theirs.id, mine.id]
    assert _post(client, "tasks", TID, "hi", headers=ah).json()["session_id"] == theirs.id
    # A task of Bob's own meeting alone is a 404 for Ann.
    solo = _owned(ctx, bob, "Solo", 9, "s1", "s2")
    key = f"{solo.id}:s1"
    assert key not in {o["key"] for o in client.get("/api/tasks?status=all", headers=ah).json()}
