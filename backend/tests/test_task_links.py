"""Tasks across meetings (services/task_links.py): linking, fan-out, reconcile, suggestions."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import task_links
from mnemosyne.services.tasks import carry_over

LONG = "Write the migration guide for the new storage format"
LONG_CLOSE = "Write the migration guide for new storage formats"


def _item(text, owner, item_id, **kw):
    return ActionItem(id=item_id, text=text, owner=owner, **kw)


def _meeting(ctx, name, items, day, attendees=None):
    s = Session(
        name=name,
        created_at=datetime(2026, 10, day, 10),
        summary="s",
        summary_data=SummaryData(action_items=items),
    )
    if attendees is not None:
        s.attendees = attendees
    return ctx.repo.save(s)


def _items(ctx, session_id):
    return ctx.repo.get(session_id).summary_data.action_items


def _history(ctx, session_id, kind):
    return [e["detail"] for e in ctx.repo.events(session_id) if e["kind"] == kind]


def _versions(ctx):
    changes, _, _ = ctx.repo.changes_since(0, limit=1000)
    return {sid: (seq, vv) for sid, seq, vv in changes}


def test_exact_link_across_meetings(ctx):
    a = _meeting(ctx, "Infra weekly", [_item("Update the docs", "Ann", "a1")], 1)
    b = _meeting(ctx, "Hiring", [_item("update the docs!", "ann", "b1")], 8)
    links = task_links.link_items(ctx, b.id, "exact")
    assert [(lk.other_session_id, lk.tier) for lk in links] == [(a.id, "exact")]
    tid = _items(ctx, b.id)[0].task_id
    assert len(tid) == 12 and _items(ctx, a.id)[0].task_id == tid
    assert _history(ctx, b.id, "task_linked") == [
        {"task": "update the docs!", "earlier": "Infra weekly", "session": a.id}
    ]
    obj = task_links.index(ctx.repo)[tid]
    assert obj.linked and [m.session_id for m in obj.mentions] == [b.id, a.id]
    assert obj.text == "update the docs!"  # the head: the newest meeting
    assert task_links.object_of(ctx.repo, a.id, "a1").key == tid


def test_related_tier_needs_a_related_meeting(ctx):
    a = _meeting(ctx, "Infra weekly", [_item(LONG, None, "a1")], 1)
    b = _meeting(ctx, "Unrelated chat", [_item(LONG_CLOSE, None, "b1")], 8)
    assert task_links.link_items(ctx, b.id, "related") == []  # titles differ, no people
    close = "Write a migration guide for the new storage format"
    c = _meeting(ctx, "Infra weekly 2026-10-09", [_item(close, None, "c1")], 9)
    assert task_links.link_items(ctx, c.id, "exact") == []  # close, not exact
    links = task_links.link_items(ctx, c.id, "related")
    assert [(lk.other_session_id, lk.tier) for lk in links] == [(a.id, "related")]


def test_related_by_people(ctx):
    people = ["Ann Lee", "Bob Stone", "Cat"]
    a = _meeting(ctx, "Kickoff", [_item(LONG, None, "a1")], 1, attendees=people)
    b = _meeting(ctx, "Follow-up", [_item(LONG_CLOSE, None, "b1")], 8, attendees=people[:2])
    assert len(task_links.link_items(ctx, b.id, "related")) == 1
    assert _items(ctx, a.id)[0].task_id == _items(ctx, b.id)[0].task_id


def test_short_texts_never_link_by_closeness(ctx):
    _meeting(ctx, "Infra weekly", [_item("Ship the release", None, "a1")], 1)
    b = _meeting(ctx, "Infra weekly", [_item("Ship the releases", None, "b1")], 8)
    assert task_links.link_items(ctx, b.id, "related") == []


def test_owners_must_be_compatible(ctx):
    _meeting(ctx, "Infra weekly", [_item("Update the docs", "Ann", "a1")], 1)
    b = _meeting(ctx, "Infra weekly", [_item("Update the docs", "Bob", "b1")], 8)
    assert task_links.link_items(ctx, b.id, "related") == []
    c = _meeting(ctx, "Infra weekly", [_item("Update the docs", "Bob and Ann", "c1")], 9)
    assert [lk.other_session_id for lk in task_links.link_items(ctx, c.id, "exact")] == [b.id]
    assert task_links.owners_compatible("Ann", None) and task_links.owners_compatible(None, "x")
    assert not task_links.owners_compatible("Ann", "Bob")


def test_a_done_task_coming_up_again_stays_new(ctx):
    a = _meeting(ctx, "Infra weekly", [_item("Update the docs", None, "a1", done=True)], 1)
    b = _meeting(ctx, "Infra weekly", [_item("Update the docs", None, "b1")], 8)
    assert task_links.link_items(ctx, b.id, "related") == []
    assert _items(ctx, a.id)[0].task_id == "" and _items(ctx, b.id)[0].task_id == ""


def test_each_candidate_is_used_once(ctx):
    a = _meeting(ctx, "Infra weekly", [_item("Update the docs", None, "a1")], 1)
    b = _meeting(
        ctx,
        "Infra weekly",
        [_item("Update the docs", None, "b1"), _item("Update the docs", None, "b2")],
        8,
    )
    links = task_links.link_items(ctx, b.id, "related")
    assert [(lk.item_id, lk.other_session_id) for lk in links] == [("b1", a.id)]
    assert _items(ctx, b.id)[1].task_id == ""


def test_link_pair_merges_two_tasks_across_three_meetings(ctx):
    a = _meeting(ctx, "One", [_item("Alpha", None, "a1")], 1)
    b = _meeting(ctx, "Two", [_item("Alpha again", None, "b1")], 2)
    c = _meeting(ctx, "Three", [_item("Beta", None, "c1", done=True)], 3)
    t1 = task_links.link_pair(ctx, (a.id, "a1"), (b.id, "b1"))
    assert _items(ctx, a.id)[0].task_id == t1 == _items(ctx, b.id)[0].task_id
    with pytest.raises(ValueError):
        task_links.link_pair(ctx, (c.id, "c1"), (c.id, "c1"))
    # c gets its own id by a link with a fourth meeting, then the two tasks merge.
    d = _meeting(ctx, "Four", [_item("Beta too", None, "d1")], 4)
    t3 = task_links.link_pair(ctx, (c.id, "c1"), (d.id, "d1"))
    assert t3 != t1
    merged = task_links.link_pair(ctx, (b.id, "b1"), (d.id, "d1"))
    ids = {_items(ctx, s.id)[0].task_id for s in (a, b, c, d)}
    assert ids == {merged}
    obj = task_links.index(ctx.repo)[merged]
    assert [m.session_id for m in obj.mentions] == [d.id, c.id, b.id, a.id]
    # The head (Four, open) wins: Three's tick is undone everywhere.
    assert not any(_items(ctx, s.id)[0].done for s in (a, b, c, d))
    assert _history(ctx, d.id, "task_linked")[-1]["earlier"] == "Two"
    with pytest.raises(LookupError):
        task_links.link_pair(ctx, (a.id, "nope"), (b.id, "b1"))


def test_unlink_leaves_no_task_of_one(ctx):
    a = _meeting(ctx, "One", [_item("Alpha", None, "a1")], 1)
    b = _meeting(ctx, "Two", [_item("Alpha", None, "b1")], 2)
    c = _meeting(ctx, "Three", [_item("Alpha", None, "c1")], 3)
    tid = task_links.link_pair(ctx, (a.id, "a1"), (b.id, "b1"))
    task_links.link_pair(ctx, (b.id, "b1"), (c.id, "c1"))
    assert task_links.unlink(ctx, c.id, "c1")
    assert _items(ctx, a.id)[0].task_id == tid == _items(ctx, b.id)[0].task_id
    assert task_links.unlink(ctx, a.id, "a1")
    assert all(_items(ctx, s.id)[0].task_id == "" for s in (a, b, c))
    assert not task_links.unlink(ctx, a.id, "a1")  # not linked any more


def test_fan_out_ticks_every_mention_and_bumps_every_meeting(ctx):
    a = _meeting(ctx, "One", [_item("Alpha", None, "a1")], 1)
    b = _meeting(ctx, "Two", [_item("Other", None, "b0"), _item("Alpha", None, "b1")], 2)
    tid = task_links.link_pair(ctx, (a.id, "a1"), (b.id, "b1"))
    before = _versions(ctx)
    assert task_links.set_done(ctx, tid, True, by="Ann") == 2
    after = _versions(ctx)
    assert _items(ctx, a.id)[0].done and _items(ctx, b.id)[1].done
    assert not _items(ctx, b.id)[0].done
    for s in (a, b):
        assert after[s.id][0] > before[s.id][0]
        assert sum(after[s.id][1].values()) == sum(before[s.id][1].values()) + 1
    # The desktop's own tick in the head meeting is not logged; the fanned-out one is.
    assert _history(ctx, a.id, "task_done") == [{"task": "Alpha", "by": "Ann"}]
    assert _history(ctx, b.id, "task_done") == []
    assert task_links.set_done(ctx, tid, True) == 0  # already done: nothing written
    assert _versions(ctx) == after
    assert task_links.set_follow(ctx, tid, True) == 2
    assert task_links.set_issue_url(ctx, tid, "https://x/1") == 2
    assert {_items(ctx, a.id)[0].issue_url, _items(ctx, b.id)[1].issue_url} == {"https://x/1"}
    with pytest.raises(LookupError):
        task_links.set_done(ctx, "nosuchtask00", True)


def test_reconcile_head_wins(ctx):
    a = _meeting(ctx, "One", [_item("Alpha", None, "a1", issue_url="https://x/9")], 1)
    b = _meeting(ctx, "Two", [_item("Alpha", None, "b1")], 2)
    tid = task_links.link_pair(ctx, (a.id, "a1"), (b.id, "b1"))
    assert _items(ctx, b.id)[0].issue_url == "https://x/9"  # an issue link is never dropped
    # A sync merge brings a tick for the older mention only.
    ctx.repo.set_action_item_flag_by_id(a.id, "a1", "done", True)
    ctx.repo.set_action_item_flag_by_id(b.id, "b1", "follow", True)
    assert task_links.reconcile(ctx) == 1
    assert not _items(ctx, a.id)[0].done and _items(ctx, a.id)[0].follow
    assert task_links.reconcile(ctx) == 0
    assert tid


def test_carry_over_keeps_the_task_id():
    old = SummaryData(action_items=[ActionItem(id="x1", text="Update the docs", task_id="t" * 12)])
    new = carry_over(old, SummaryData(action_items=[ActionItem(text="Update the docs.")]))
    assert new.action_items[0].task_id == "t" * 12


def test_suggestions_and_dismiss(ctx):
    a = _meeting(ctx, "Infra weekly", [_item(LONG, None, "a1")], 1)
    b = _meeting(ctx, "Infra weekly", [_item(LONG_CLOSE, None, "b1")], 8)
    _meeting(ctx, "Elsewhere", [_item(LONG_CLOSE, None, "x1")], 9)  # not related to either
    got = task_links.suggestions(ctx)
    assert [(s.a.session_id, s.b.session_id) for s in got] == [(b.id, a.id)]
    assert 0.85 <= got[0].score <= 1
    task_links.dismiss(ctx, (a.id, "a1"), (b.id, "b1"))
    assert task_links.suggestions(ctx) == []
    task_links.dismiss(ctx, (b.id, "b1"), (a.id, "a1"))  # again: stored once
    assert len(task_links._dismissed(ctx.repo)) == 1


def test_off_setting_exists(settings):
    assert settings.task_linking == "related"


# ---- team mode ------------------------------------------------------------------------------


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name in ("Ann Lee", "Bob Stone"):
        people[name.split()[0]] = ctx.users.add(
            name, f"{name.split()[0].lower()}@team.test", "member"
        )
    with TestClient(app):
        yield ctx, people


def _as(user):
    return access.current.set(access.Principal(user.id, user.name, user.role))


def test_team_fan_out_reaches_a_meeting_the_caller_cannot_see(team):
    ctx, people = team
    ann, bob = people["Ann"], people["Bob"]
    reset = _as(bob)
    try:
        hidden = _meeting(ctx, "Bob's", [_item("Alpha", None, "h1")], 1)
    finally:
        access.current.reset(reset)
    reset = _as(ann)
    try:
        mine = _meeting(ctx, "Ann's", [_item("Alpha", None, "m1")], 2)
    finally:
        access.current.reset(reset)
    tid = task_links.link_pair(ctx, (hidden.id, "h1"), (mine.id, "m1"))  # no principal: all
    reset = _as(ann)
    try:
        visible = ctx.repo.visible_ids()
        assert hidden.id not in visible
        obj = task_links.index(ctx.repo, visible)[tid]
        assert [m.session_id for m in obj.mentions] == [mine.id] and not obj.linked
        assert task_links.set_done(ctx, tid, True, by="Ann Lee") == 2
        with pytest.raises(LookupError):
            task_links.link_pair(ctx, (hidden.id, "h1"), (mine.id, "m1"))
        assert task_links.suggestions(ctx) == []
    finally:
        access.current.reset(reset)
    assert _items(ctx, hidden.id)[0].done
    assert _history(ctx, hidden.id, "task_done") == [{"task": "Alpha", "by": "Ann Lee"}]
    # The internal writer never touches text or links.
    with pytest.raises(ValueError):
        ctx.repo.fan_out_action_item_fields(hidden.id, "h1", task_id="")


def test_an_old_meeting_does_not_link_to_a_newer_meetings_open_twin(ctx):
    old = _meeting(ctx, "Old", [_item("Send the budget to finance", None, "o1")], 1)
    new = _meeting(ctx, "New", [_item("Send the budget to finance", None, "n1")], 20)
    assert task_links.link_items(ctx, old.id) == []  # e.g. the old meeting is re-summarized
    assert _items(ctx, old.id)[0].task_id == "" and _items(ctx, new.id)[0].task_id == ""
    links = task_links.link_items(ctx, new.id)  # the newer one does join the older one
    assert [link.other_session_id for link in links] == [old.id]
    assert _items(ctx, old.id)[0].task_id == _items(ctx, new.id)[0].task_id != ""
