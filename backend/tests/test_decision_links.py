"""Decisions across meetings (services/decision_links.py): auto-link, supersede, routes."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import Session, SummaryData
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import decision_links, sharing
from tests.conftest import run_summarize

OCT = "We ship in October"
NOV = "We ship in November"


def _meeting(ctx, name, decisions, day, **kw):
    """decisions: [(id, text)]"""
    return ctx.repo.save(
        Session(
            name=name,
            created_at=datetime(2026, 10, day, 10),
            summary="s",
            summary_data=SummaryData(
                decisions=[t for _, t in decisions], decision_ids=[i for i, _ in decisions]
            ),
            **kw,
        )
    )


def _links(ctx, sid):
    return ctx.repo.get(sid).summary_data.decision_links


def _history(ctx, sid):
    return [e["detail"] for e in ctx.repo.events(sid) if e["kind"] == "decision_linked"]


def _ref(m, decision_id):
    return {"session_id": m.id, "decision_id": decision_id}


def test_exact_auto_link_across_two_meetings(ctx):
    a = _meeting(ctx, "Plan 1", [("d1", OCT), ("d2", "Hire a designer")], 1)
    b = _meeting(ctx, "Plan 2", [("e1", "we ship in october!"), ("e2", "Something new")], 8)
    made = decision_links.link_items(ctx, b.id)
    assert [(d, other) for d, _, other in made] == [("e1", a.id)]
    obj_id = _links(ctx, b.id)["e1"]
    assert len(obj_id) == 12 and _links(ctx, a.id) == {"d1": obj_id}
    assert _history(ctx, b.id) == [
        {"decision": "we ship in october!", "earlier": "Plan 1", "session": a.id}
    ]
    obj = decision_links.index(ctx.repo)[obj_id]
    assert obj.linked and [m.session_id for m in obj.mentions] == [b.id, a.id]
    assert obj.text == "we ship in october!"  # head = the newest mention
    # Unlinked decisions are objects of their own.
    assert set(decision_links.index(ctx.repo)) == {obj_id, f"{a.id}:d2", f"{b.id}:e2"}
    # Running it again changes nothing.
    assert decision_links.link_items(ctx, b.id) == []
    assert len(_history(ctx, b.id)) == 1


def test_different_wording_is_not_auto_linked(ctx):
    a = _meeting(ctx, "Plan 1", [("d1", OCT)], 1)
    b = _meeting(ctx, "Plan 2", [("e1", NOV)], 8)
    assert decision_links.link_items(ctx, b.id) == []
    assert _links(ctx, a.id) == {} and _links(ctx, b.id) == {}


def test_each_object_is_used_once_and_a_meeting_does_not_link_to_itself(ctx):
    a = _meeting(ctx, "Plan 1", [("d1", OCT)], 1)
    b = _meeting(ctx, "Plan 2", [("e1", OCT), ("e2", OCT)], 8)
    made = decision_links.link_items(ctx, b.id)
    assert [d for d, _, _ in made] == ["e1"]
    assert list(_links(ctx, b.id)) == ["e1"]
    assert decision_links.link_items(ctx, a.id) == []  # nothing in an earlier meeting is newer


def test_a_third_meeting_joins_the_object_and_matches_the_head(ctx):
    a = _meeting(ctx, "One", [("d1", OCT)], 1)
    b = _meeting(ctx, "Two", [("e1", OCT)], 8)
    decision_links.link_items(ctx, b.id)
    c = _meeting(ctx, "Three", [("f1", OCT)], 15)
    decision_links.link_items(ctx, c.id)
    ids = {_links(ctx, s.id)[k] for s, k in ((a, "d1"), (b, "e1"), (c, "f1"))}
    assert len(ids) == 1
    assert [m.session_id for m in decision_links.index(ctx.repo)[ids.pop()].mentions] == [
        c.id,
        b.id,
        a.id,
    ]


def test_manual_supersede_makes_the_newer_wording_the_head_and_unlink_undoes_it(ctx):
    m1 = _meeting(ctx, "M1", [("d1", OCT)], 1)
    m2 = _meeting(ctx, "M2", [("x1", "Unrelated")], 8)
    m3 = _meeting(ctx, "M3", [("f1", NOV)], 15)
    key = decision_links.link_pair(ctx, (m1.id, "d1"), (m3.id, "f1"))
    obj = decision_links.index(ctx.repo)[key]
    assert obj.text == NOV and [m.text for m in obj.mentions] == [NOV, OCT]
    assert _history(ctx, m3.id) == [{"decision": NOV, "earlier": "M1", "session": m1.id}]
    # Linking the same pair again is a no-op; one meeting twice is refused.
    assert decision_links.link_pair(ctx, (m3.id, "f1"), (m1.id, "d1")) == key
    assert len(_history(ctx, m3.id)) == 1
    with pytest.raises(ValueError):
        decision_links.link_pair(ctx, (m1.id, "d1"), (m1.id, "d1"))
    with pytest.raises(LookupError):
        decision_links.link_pair(ctx, (m1.id, "d1"), (m2.id, "nope"))
    # Unlinking one of two leaves neither linked.
    assert decision_links.unlink(ctx, m1.id, "d1")
    assert _links(ctx, m1.id) == {} and _links(ctx, m3.id) == {}
    assert not decision_links.unlink(ctx, m1.id, "d1")
    assert all(not o.linked for o in decision_links.index(ctx.repo).values())


def test_merging_two_objects_and_unlinking_one_of_three(ctx):
    a = _meeting(ctx, "A", [("d1", "One")], 1)
    b = _meeting(ctx, "B", [("e1", "Two")], 8)
    c = _meeting(ctx, "C", [("f1", "Three")], 15)
    d = _meeting(ctx, "D", [("g1", "Four")], 22)
    decision_links.link_pair(ctx, (a.id, "d1"), (b.id, "e1"))
    decision_links.link_pair(ctx, (c.id, "f1"), (d.id, "g1"))
    key = decision_links.link_pair(ctx, (b.id, "e1"), (c.id, "f1"))
    obj = decision_links.index(ctx.repo)[key]
    assert [m.session_id for m in obj.mentions] == [d.id, c.id, b.id, a.id]
    assert decision_links.unlink(ctx, b.id, "e1")
    obj = decision_links.index(ctx.repo)[key]
    assert [m.session_id for m in obj.mentions] == [d.id, c.id, a.id]
    assert _links(ctx, b.id) == {}


def test_links_survive_a_resummarize(client, ctx, fake_provider):
    a = _meeting(ctx, "Plan 1", [("d1", OCT)], 1)
    b = ctx.repo.save(
        Session(
            name="Plan 2",
            created_at=datetime(2026, 10, 8, 10),
            transcript=[TranscriptSegment(text="ship in october", speaker="Ann", start=0, end=2)],
        )
    )
    fake_provider.summary = json.dumps(
        {"summary": ["Notes."], "decisions": [OCT], "open_questions": [], "action_items": []}
    )
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    (obj_id,) = _links(ctx, b.id).values()
    assert _links(ctx, a.id) == {"d1": obj_id}
    assert len(_history(ctx, b.id)) == 1
    assert run_summarize(client, b.id, {"provider": "fake"})["status"] == "completed"
    data = ctx.repo.get(b.id).summary_data
    assert data.decision_links == {data.decision_ids[0]: obj_id}
    assert len(_history(ctx, b.id)) == 1  # kept, not linked a second time


def test_a_hand_edit_that_adds_the_same_decision_links_it(client, ctx):
    a = _meeting(ctx, "Plan 1", [("d1", OCT)], 1)
    b = _meeting(ctx, "Plan 2", [("e1", "Other")], 8)
    body = {
        "summary": "Notes.",
        "topics": [],
        "decisions": [{"text": "Other", "id": "e1"}, {"text": OCT}],
        "open_questions": [],
        "action_items": [],
        "chapters": [],
        "client_facts": [],
    }
    assert client.put(f"/api/sessions/{b.id}/summary", json=body).status_code == 200
    data = ctx.repo.get(b.id).summary_data
    twin = data.decision_ids[data.decisions.index(OCT)]
    assert list(data.decision_links) == [twin]
    assert data.decision_links[twin] == _links(ctx, a.id)["d1"]
    # Dropping the decision again drops its link (the other meeting keeps its own entry).
    body["decisions"] = [{"text": "Other", "id": "e1"}]
    assert client.put(f"/api/sessions/{b.id}/summary", json=body).status_code == 200
    assert ctx.repo.get(b.id).summary_data.decision_links == {}


def test_decision_linking_follows_task_linking_off_and_never_fails_the_job(
    client, ctx, fake_provider, monkeypatch
):
    ctx.settings.task_linking = "off"
    a = _meeting(ctx, "Plan 1", [("d1", OCT)], 1)
    b = _meeting(ctx, "Plan 2", [("e1", OCT)], 8)
    from mnemosyne.services.summary_edit import link_quietly

    assert link_quietly(ctx, b.id) is None and _links(ctx, a.id) == {}
    ctx.settings.task_linking = "exact"

    def boom(*a, **k):
        raise RuntimeError("db is sad")

    monkeypatch.setattr(decision_links, "link_items", boom)
    assert link_quietly(ctx, b.id) is None  # logged, not raised


# ---- routes -----------------------------------------------------------------------------------


def test_routes_list_link_unlink_and_session_links(client, ctx):
    m1 = _meeting(ctx, "M1", [("d1", OCT)], 1)
    m3 = _meeting(ctx, "M3", [("f1", NOV), ("f2", "Alone")], 15)
    assert len(client.get("/api/decisions").json()) == 3
    assert client.get("/api/decisions", params={"linked": "true"}).json() == []

    r = client.post("/api/decisions/link", json={"a": _ref(m1, "d1"), "b": _ref(m3, "f1")})
    assert r.status_code == 200, r.text
    obj = r.json()
    assert obj["linked"] and obj["text"] == NOV
    assert [m["session_id"] for m in obj["mentions"]] == [m3.id, m1.id]
    key = obj["key"]
    assert [o["key"] for o in client.get("/api/decisions", params={"linked": "true"}).json()] == [
        key
    ]
    assert len(client.get("/api/decisions", params={"linked": "false"}).json()) == 1
    for bad, code in (
        ({"a": _ref(m1, "d1"), "b": _ref(m1, "d1")}, 400),
        ({"a": _ref(m1, "d1"), "b": _ref(m3, "zz")}, 404),
    ):
        assert client.post("/api/decisions/link", json=bad).status_code == code

    later = client.get(f"/api/sessions/{m1.id}/links").json()["decisions"]
    assert later["d1"]["key"] == key and later["d1"]["earlier"] == []
    assert [m["text"] for m in later["d1"]["later"]] == [NOV]
    newer = client.get(f"/api/sessions/{m3.id}/links").json()["decisions"]
    assert [m["text"] for m in newer["f1"]["earlier"]] == [OCT]
    assert newer["f2"] == {"key": f"{m3.id}:f2", "earlier": [], "later": []}

    gone = client.delete(f"/api/decisions/{key}/mentions/{m1.id}/d1")
    assert gone.status_code == 200 and not gone.json()["linked"]
    assert gone.json()["key"] == f"{m1.id}:d1"
    assert client.delete(f"/api/decisions/{key}/mentions/{m1.id}/d1").status_code == 404


# ---- team mode --------------------------------------------------------------------------------


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


def _owned(ctx, user, name, decisions, day):
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        return _meeting(ctx, name, decisions, day)
    finally:
        access.current.reset(reset)


def test_a_member_only_sees_and_links_what_they_can_read(team):
    client, ctx, people = team
    (ann, ah), (bob, _) = people["Ann"], people["Bob"]
    mine = _owned(ctx, ann, "Ann's", [("a1", OCT)], 1)
    theirs = _owned(ctx, bob, "Bob's", [("b1", OCT)], 8)
    secret = _owned(ctx, bob, "Bob secret", [("s1", "Secret")], 9)
    got = client.get("/api/decisions", headers=ah).json()
    assert [o["mentions"][0]["session_id"] for o in got] == [mine.id]
    # Not shared: the pair cannot be linked, and the secret one is not even found.
    body = {"a": _ref(mine, "a1"), "b": _ref(theirs, "b1")}
    assert client.post("/api/decisions/link", json=body, headers=ah).status_code == 404
    assert (
        client.post(
            "/api/decisions/link", json={**body, "b": _ref(secret, "s1")}, headers=ah
        ).status_code
        == 404
    )
    # Auto-link run as Ann ignores Bob's meeting; shared, it links (a reader may link).
    reset = access.current.set(access.Principal(ann.id, ann.name, ann.role))
    try:
        assert decision_links.link_items(ctx, mine.id) == []
        sharing.set_shares(ctx, theirs.id, {ann.id})
        both = client.get("/api/decisions", headers=ah).json()
        assert sorted(len(o["mentions"]) for o in both) == [1, 1]
        assert client.post("/api/decisions/link", json=body, headers=ah).status_code == 200
    finally:
        access.current.reset(reset)
    obj = client.get("/api/decisions", params={"linked": "true"}, headers=ah).json()
    assert [m["session_id"] for m in obj[0]["mentions"]] == [theirs.id, mine.id]
    # The secret meeting never appears in anything Ann reads.
    assert all(
        secret.id not in {m["session_id"] for m in o["mentions"]}
        for o in client.get("/api/decisions", headers=ah).json()
    )


def test_an_old_meeting_does_not_link_to_a_newer_one(ctx):
    a = _meeting(ctx, "Old", [("d1", OCT)], 1)
    _meeting(ctx, "New", [("e1", OCT)], 20)
    assert decision_links.link_items(ctx, a.id) == []
    assert _links(ctx, a.id) == {}
