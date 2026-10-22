"""Comments on a team server (services/comments.py): who may comment, edit, delete, resolve;
@mentions as user ids in the `comments` event."""

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import Session
from mnemosyne.models.transcript import TranscriptSegment
from mnemosyne.services import sharing


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [
        ("Admin", "admin"),
        ("Ann Lee", "member"),
        ("Bob Stone", "member"),
        ("Cat", "member"),
        ("Rita Vance", "reviewer"),
    ]:
        user = ctx.users.add(name, f"{name.split()[0].lower()}@team.test", role)
        code, _ = ctx.users.invite(user.id)
        people[name.split()[0]] = (
            user,
            {"Authorization": f"Bearer {ctx.users.redeem(code, 'test')[1]}"},
        )
    with TestClient(app) as client:
        yield client, ctx, people


def _meeting(ctx, people, owner) -> str:
    user = people[owner][0]
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        session = Session(
            name="Planning",
            transcript=[TranscriptSegment(text="the quarterly plan", speaker="A", start=0, end=2)],
            summary="We planned.",
        )
        return ctx.repo.save(session).id
    finally:
        access.current.reset(reset)


def _url(sid, rest=""):
    return f"/api/sessions/{sid}/comments{rest}"


def _comment(client, sid, who, **body):
    return client.post(
        _url(sid), json={"text": "Hm", "target": "segment", "idx": 0, **body}, headers=who
    )


def test_a_shared_reader_comments_and_a_stranger_sees_nothing(team):
    client, ctx, people = team
    sid = _meeting(ctx, people, "Ann")
    bob, cat = people["Bob"][1], people["Cat"][1]
    assert _comment(client, sid, bob).status_code == 404
    assert client.get(_url(sid), headers=bob).status_code == 404
    sharing.set_shares(ctx, sid, {people["Bob"][0].id})
    r = _comment(client, sid, bob, text="Which quarter?")
    assert r.status_code == 200
    c = r.json()
    assert (c["author_id"], c["author"]) == (people["Bob"][0].id, "Bob Stone")
    assert c["can_edit"] and c["can_delete"] and c["can_resolve"]  # his own thread
    got = client.get(_url(sid), headers=bob).json()
    assert got["can_comment"] and len(got["threads"]) == 1
    # Cat: not shared with her, everything answers as if there were nothing.
    assert client.get(_url(sid), headers=cat).status_code == 404
    for call in (
        lambda: client.patch(_url(sid, f"/{c['id']}"), json={"text": "x"}, headers=cat),
        lambda: client.delete(_url(sid, f"/{c['id']}"), headers=cat),
        lambda: client.post(_url(sid, f"/{c['id']}/resolve"), headers=cat),
    ):
        assert call().status_code == 404


def test_a_reviewer_comments_on_a_members_meeting(team):
    client, ctx, people = team
    sid = _meeting(ctx, people, "Ann")
    r = _comment(client, sid, people["Rita"][1], text="Please check the numbers")
    assert r.status_code == 200 and r.json()["author"] == "Rita Vance"
    # The reviewer's own thread is hers to resolve; Ann owns the meeting, so she may too.
    ann_view = client.get(_url(sid), headers=people["Ann"][1]).json()["threads"][0]["root"]
    assert not ann_view["can_edit"] and ann_view["can_delete"] and ann_view["can_resolve"]


def test_only_the_author_edits_and_the_owner_resolves(team):
    client, ctx, people = team
    sid = _meeting(ctx, people, "Ann")
    sharing.set_shares(ctx, sid, {sharing.EVERYONE})
    ann, bob, cat, admin = (people[n][1] for n in ("Ann", "Bob", "Cat", "Admin"))
    mine = _comment(client, sid, bob, text="Bob's thread").json()
    other = _comment(client, sid, cat, text="Cat's thread").json()

    # Edit: the author only (an admin too); the meeting's owner may not put words in his mouth.
    assert (
        client.patch(_url(sid, f"/{mine['id']}"), json={"text": "x"}, headers=ann).status_code
        == 403
    )
    assert (
        client.patch(_url(sid, f"/{mine['id']}"), json={"text": "x"}, headers=cat).status_code
        == 403
    )
    r = client.patch(
        _url(sid, f"/{mine['id']}"), json={"text": "Bob's thread, edited"}, headers=bob
    )
    assert r.status_code == 200
    r = client.patch(_url(sid, f"/{mine['id']}"), json={"text": "By the admin"}, headers=admin)
    assert r.status_code == 200

    # Resolve: the owner may resolve anyone's; a reader only their own thread.
    assert client.post(_url(sid, f"/{other['id']}/resolve"), headers=bob).status_code == 403
    assert client.post(_url(sid, f"/{mine['id']}/resolve"), headers=bob).status_code == 200
    assert client.post(_url(sid, f"/{mine['id']}/reopen"), headers=bob).status_code == 200
    r = client.post(_url(sid, f"/{other['id']}/resolve"), headers=ann)
    assert r.status_code == 200 and r.json()["resolved_by"] == "Ann Lee"
    assert client.post(_url(sid, f"/{other['id']}/reopen"), headers=bob).status_code == 403

    # A reply in Cat's thread by Bob: Cat (the thread's author) may resolve, Bob may not.
    reply = client.post(
        _url(sid), json={"text": "Agreed", "parent_id": other["id"]}, headers=bob
    ).json()
    assert not reply["can_resolve"]
    assert client.post(_url(sid, f"/{other['id']}/reopen"), headers=cat).status_code == 200

    # Delete: the author, the meeting's owner or an admin; not another reader.
    assert client.delete(_url(sid, f"/{reply['id']}"), headers=cat).status_code == 403
    assert client.delete(_url(sid, f"/{reply['id']}"), headers=ann).status_code == 200
    assert client.delete(_url(sid, f"/{mine['id']}"), headers=cat).status_code == 403
    assert client.delete(_url(sid, f"/{mine['id']}"), headers=bob).status_code == 200
    assert client.delete(_url(sid, f"/{other['id']}"), headers=admin).status_code == 200

    views = {
        e["kind"]: e["detail"]["by"]
        for e in ctx.repo.events(sid)
        if e["kind"] in ("comment_resolved", "comment_reopened")
    }
    assert views == {"comment_resolved": "Ann Lee", "comment_reopened": "Cat"}


def test_mentions_are_user_ids_in_the_event(team):
    client, ctx, people = team
    sid = _meeting(ctx, people, "Ann")
    sharing.set_shares(ctx, sid, {sharing.EVERYONE})
    ann, bob, cat = (people[n][0].id for n in ("Ann", "Bob", "Cat"))
    queue = ctx.bus.subscribe()
    c = _comment(
        client,
        sid,
        people["Ann"][1],
        text="@bob stone and @Cat, see this (mail ann@team.test, not @nobody)",
    ).json()
    assert c["mentions"] == [bob, cat]
    # First names do when nobody else has them; an edit announces only the new ones.
    r = client.patch(
        _url(sid, f"/{c['id']}"), json={"text": "@Bob, @cat and @ANN!"}, headers=people["Ann"][1]
    )
    assert r.json()["mentions"] == [bob, cat, ann]
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if event["type"] == "comments":
            events.append(event)
    ctx.bus.unsubscribe(queue)
    assert events == [
        {"type": "comments", "session_id": sid, "mentions": [bob, cat]},
        {"type": "comments", "session_id": sid, "mentions": [ann]},
    ]


def test_the_comments_event_reaches_only_those_who_may_read(team):
    client, ctx, people = team
    sid = _meeting(ctx, people, "Ann")
    from mnemosyne.api.websocket import visibility

    seen = {}
    for name in ("Ann", "Bob", "Rita"):
        user = people[name][0]
        reset = access.current.set(access.Principal(user.id, user.name, user.role))
        try:
            seen[name] = visibility(ctx)({"type": "comments", "session_id": sid, "mentions": []})
        finally:
            access.current.reset(reset)
    assert seen == {"Ann": True, "Bob": False, "Rita": True}
