"""Task objects over HTTP (routes/tasks.py): /api/tasks, links, suggestions, session links."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import sharing

TID = "aaaaaaaaaaaa"
LONG = "Write the migration guide for the new storage format"
LONG_CLOSE = "Write the migration guide for new storage formats"


def _item(item_id, text="Update the docs", owner="Ann", **kw):
    return ActionItem(id=item_id, text=text, owner=owner, **kw)


def _meeting(ctx, name, items, day, **kw):
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, day, 10),
            summary="s",
            summary_data=SummaryData(action_items=items),
            **kw,
        )
    )


def _history(ctx, session_id, kind="task_done"):
    return [e["detail"] for e in ctx.repo.events(session_id) if e["kind"] == kind]


def _linked_pair(ctx):
    old = _meeting(ctx, "Infra weekly", [_item("a1", task_id=TID)], 1)
    new = _meeting(ctx, "Infra weekly 2", [_item("b1", task_id=TID), _item("b2", "Other", None)], 8)
    return old, new


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_linked_task_is_one_object_with_two_mentions(client, ctx):
    old, new = _linked_pair(ctx)
    got = client.get("/api/tasks").json()
    by_key = {o["key"]: o for o in got}
    obj = by_key[TID]
    assert obj["linked"] and obj["task_id"] == TID
    assert [m["session_id"] for m in obj["mentions"]] == [new.id, old.id]
    assert set(by_key) == {TID, f"{new.id}:b2"}  # the other item is an object of its own
    assert obj["can_edit"] is True


def test_filters_follow_the_action_item_list(client, ctx):
    ctx.settings.local_speaker_name = "Ann"
    _linked_pair(ctx)
    assert [o["key"] for o in client.get("/api/tasks", params={"scope": "mine"}).json()] == [TID]
    assert client.get("/api/tasks", params={"status": "done"}).json() == []
    assert len(client.get("/api/tasks", params={"status": "all"}).json()) == 2
    owner = client.get("/api/tasks", params={"owner": "ann"}).json()
    assert [o["key"] for o in owner] == [TID] and owner[0]["mine"] is True


def test_patch_by_index_ticks_every_mention_and_logs_once_per_meeting(client, ctx):
    old, new = _linked_pair(ctx)
    q = ctx.bus.subscribe()
    r = client.patch(f"/api/sessions/{old.id}/action-items/0", json={"done": True})
    assert r.status_code == 200 and r.json()["done"] is True
    for s in (old, new):
        assert ctx.repo.get(s.id).summary_data.action_items[0].done
    assert len(_history(ctx, old.id)) == 1  # the meeting acted on: one line, not two
    assert len(_history(ctx, new.id)) == 0  # the desktop owner's own head meeting: none
    sessions = [e["session_id"] for e in _drain(q) if e["type"] == "session"]
    assert sessions.count(new.id) == 1 and sessions.count(old.id) == 1  # route's + fan-out's
    # Starring follows the task as well.
    client.patch(f"/api/sessions/{new.id}/action-items/0", json={"follow": True})
    assert ctx.repo.get(old.id).summary_data.action_items[0].follow
    # Reopening from the head.
    client.patch(f"/api/sessions/{new.id}/action-items/0", json={"done": False})
    assert not ctx.repo.get(old.id).summary_data.action_items[0].done
    assert len(_history(ctx, old.id, "task_reopened")) == 1


def test_patch_by_index_without_a_link_is_as_before(client, ctx):
    m = _meeting(ctx, "Solo", [_item("s1"), ActionItem(text="no id")], 3)
    client.patch(f"/api/sessions/{m.id}/action-items/0", json={"done": True})
    client.patch(f"/api/sessions/{m.id}/action-items/1", json={"done": True, "follow": True})
    items = ctx.repo.get(m.id).summary_data.action_items
    assert items[0].done and items[1].done and items[1].follow
    assert _history(ctx, m.id) == []


def test_patch_task_by_key(client, ctx):
    old, new = _linked_pair(ctx)
    r = client.patch(f"/api/tasks/{TID}", json={"done": True, "follow": True})
    assert r.status_code == 200
    assert r.json()["done"] and r.json()["follow"]
    assert all(m["done"] for m in r.json()["mentions"])
    assert len(_history(ctx, old.id)) == 1
    assert client.patch("/api/tasks/nonesuch", json={"done": True}).status_code == 404


def test_link_and_unlink(client, ctx):
    a = _meeting(ctx, "One", [_item("a1", "Send the invoice")], 1)
    b = _meeting(ctx, "Two", [_item("b1", "Invoice to Acme")], 8)
    ref = lambda m, i: {"session_id": m.id, "item_id": i}  # noqa: E731
    r = client.post("/api/tasks/link", json={"a": ref(a, "a1"), "b": ref(b, "b1")})
    assert r.status_code == 200
    obj = r.json()
    assert obj["linked"] and [m["session_id"] for m in obj["mentions"]] == [b.id, a.id]
    key = obj["key"]
    assert (
        client.post("/api/tasks/link", json={"a": ref(a, "a1"), "b": ref(a, "a1")}).status_code
        == 400
    )
    assert (
        client.post("/api/tasks/link", json={"a": ref(a, "a1"), "b": ref(b, "zz")}).status_code
        == 404
    )
    links = client.get(f"/api/sessions/{b.id}/links").json()
    assert links["decisions"] == {}
    assert [m["session_id"] for m in links["items"]["b1"]["earlier"]] == [a.id]
    assert links["items"]["b1"]["later"] == [] and links["items"]["b1"]["key"] == key
    back = client.get(f"/api/sessions/{a.id}/links").json()
    assert [m["session_id"] for m in back["items"]["a1"]["later"]] == [b.id]
    assert client.get("/api/sessions/nope/links").status_code == 404

    q = ctx.bus.subscribe()
    gone = client.delete(f"/api/tasks/{key}/mentions/{a.id}/a1")
    assert gone.status_code == 200 and not gone.json()["linked"]
    assert gone.json()["key"] == f"{a.id}:a1"
    assert {e["session_id"] for e in _drain(q) if e["type"] == "session"} >= {a.id, b.id}
    assert client.get(f"/api/sessions/{b.id}/links").json()["items"]["b1"]["earlier"] == []
    assert client.delete(f"/api/tasks/{key}/mentions/{a.id}/a1").status_code == 404


def test_suggestions_apply_and_dismiss(client, ctx):
    a = _meeting(ctx, "Infra weekly", [_item("a1", LONG, None)], 1)
    b = _meeting(ctx, "Infra weekly 2026-10-09", [_item("b1", LONG_CLOSE, None)], 8)
    sug = client.get("/api/tasks/suggestions").json()
    assert len(sug) == 1 and sug[0]["a"]["session_id"] == b.id and sug[0]["b"]["session_id"] == a.id
    pair = {"a": {"session_id": b.id, "item_id": "b1"}, "b": {"session_id": a.id, "item_id": "a1"}}
    assert client.post("/api/tasks/suggestions/dismiss", json=pair).status_code == 204
    assert client.get("/api/tasks/suggestions").json() == []
    bad = {"a": pair["a"], "b": {"session_id": a.id, "item_id": "zz"}}
    assert client.post("/api/tasks/suggestions/dismiss", json=bad).status_code == 404

    c = _meeting(ctx, "Infra weekly 2026-10-16", [_item("c1", LONG_CLOSE, None)], 15)
    d = _meeting(ctx, "Infra weekly 2026-10-02", [_item("d1", LONG, None)], 2)
    ref = {"a": {"session_id": c.id, "item_id": "c1"}, "b": {"session_id": d.id, "item_id": "d1"}}
    assert any(
        {s["a"]["item_id"], s["b"]["item_id"]} == {"c1", "d1"}
        for s in client.get("/api/tasks/suggestions").json()
    )
    r = client.post("/api/tasks/suggestions/apply", json=ref)
    assert r.status_code == 200 and r.json()["linked"]
    assert not any(
        {s["a"]["item_id"], s["b"]["item_id"]} == {"c1", "d1"}
        for s in client.get("/api/tasks/suggestions").json()
    )


# ---- team mode ------------------------------------------------------------------------------


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


def _owned(ctx, user, name, items, day):
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        return _meeting(ctx, name, items, day)
    finally:
        access.current.reset(reset)


def test_a_member_sees_only_the_mentions_they_may_read(team):
    client, ctx, people = team
    (ann, ah), (bob, _) = people["Ann"], people["Bob"]
    mine = _owned(ctx, ann, "Ann's", [_item("a1", task_id=TID)], 1)
    theirs = _owned(ctx, bob, "Bob's", [_item("b1", task_id=TID)], 8)
    got = client.get("/api/tasks", headers=ah).json()
    assert [(o["key"], len(o["mentions"]), o["linked"]) for o in got] == [(TID, 1, False)]
    assert got[0]["mentions"][0]["session_id"] == mine.id and got[0]["can_edit"] is True
    assert client.get(f"/api/sessions/{mine.id}/links", headers=ah).json()["items"]["a1"] == {
        "key": TID,
        "earlier": [],
        "later": [],
    }
    # Ticking reaches Bob's meeting too (same task); his history names Ann.
    r = client.patch(f"/api/tasks/{TID}", json={"done": True}, headers=ah)
    assert r.status_code == 200 and len(r.json()["mentions"]) == 1
    assert ctx.repo.get(theirs.id).summary_data.action_items[0].done
    assert _history(ctx, theirs.id) == [{"task": "Update the docs", "by": "Ann Lee"}]
    # Shared, Ann reads Bob's meeting: two mentions, but the head is not hers to edit.
    sharing.set_shares(ctx, theirs.id, {ann.id})
    both = client.get("/api/tasks", params={"status": "all"}, headers=ah).json()
    assert len(both[0]["mentions"]) == 2 and both[0]["can_edit"] is False
    # A stranger sees nothing of it.
    solo = _owned(ctx, bob, "Bob's own", [_item("c1", "Private")], 9)
    keys = [o["key"] for o in client.get("/api/tasks", params={"status": "all"}, headers=ah).json()]
    assert f"{solo.id}:c1" not in keys
    assert (
        client.patch(f"/api/tasks/{solo.id}:c1", json={"done": True}, headers=ah).status_code == 404
    )


def test_linking_into_meetings_you_cannot_see(team):
    client, ctx, people = team
    (ann, ah), (bob, _) = people["Ann"], people["Bob"]
    t1, t2 = "111111111aaa", "222222222bbb"
    _owned(ctx, ann, "Ann's", [_item("a1", task_id=t1)], 1)
    _owned(ctx, bob, "Bob hidden 1", [_item("h1", task_id=t1)], 2)
    shared = _owned(ctx, bob, "Bob shared", [_item("s1", task_id=t2)], 3)
    _owned(ctx, bob, "Bob hidden 2", [_item("h2", task_id=t2)], 4)
    secret = _owned(ctx, bob, "Bob secret", [_item("x1", "Secret")], 5)
    sharing.set_shares(ctx, shared.id, {ann.id})
    ann_item = next(
        {"session_id": s.id, "item_id": "a1"}
        for s in ctx.repo.list_summaries()
        if s.name == "Ann's"
    )
    both_hidden = client.post(
        "/api/tasks/link",
        json={"a": ann_item, "b": {"session_id": shared.id, "item_id": "s1"}},
        headers=ah,
    )
    assert both_hidden.status_code == 403
    unseen = client.post(
        "/api/tasks/link",
        json={"a": ann_item, "b": {"session_id": secret.id, "item_id": "x1"}},
        headers=ah,
    )
    assert unseen.status_code == 404
    assert (
        client.post(
            "/api/tasks/suggestions/dismiss",
            json={"a": ann_item, "b": {"session_id": secret.id, "item_id": "x1"}},
            headers=ah,
        ).status_code
        == 404
    )
