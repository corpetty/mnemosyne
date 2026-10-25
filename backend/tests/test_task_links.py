"""Tasks across meetings (services/task_links.py): linking, fan-out, reconcile, suggestions."""

import base64
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import notes_items, shared_notes, task_links
from mnemosyne.services.tasks import carry_over
from tests import fake_link
from tests.fake_link import doc_frame

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


# ---- action items from the notes are mentions (phase 11) -----------------------------------

ADA = "[@Ada Lovelace](mnemosyne:person/Ada%20Lovelace)"


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)
    return fake_link.install(tmp_path, monkeypatch)


def _typed(ctx, sid, fid, *ops):
    frame = base64.b64decode(doc_frame(fid, *ops))
    shared_notes.accept_frames(ctx, sid, [frame], "u-ada", "Ada", can_edit=True)
    shared_notes.fold_blocking(ctx, sid)


def _notes_meeting(ctx, name, day, text):
    s = ctx.repo.save(Session(name=name, created_at=datetime(2026, 10, day, 10)))
    _typed(ctx, s.id, "f1", ["set", f"- [ ] {ADA}: {text}\n"])
    return s


def test_a_notes_item_links_with_an_older_open_task_by_exact_text(ctx, sidecar):
    old = _meeting(ctx, "Infra weekly", [_item("Send the deck", "Ada Lovelace", "a1")], 1)
    done = _meeting(ctx, "Older", [_item("Send the deck", "Ada Lovelace", "d1", done=True)], 2)
    new = _notes_meeting(ctx, "Planning", 8, "send the deck!")  # the fold links it
    (item,) = ctx.repo.notes_items(new.id)
    assert item.text == "send the deck!" and len(item.task_id) == 12
    assert _items(ctx, old.id)[0].task_id == item.task_id
    assert _items(ctx, done.id)[0].task_id == ""  # a done task stays done
    assert _history(ctx, new.id, "task_linked") == [
        {"task": "send the deck!", "earlier": "Infra weekly", "session": old.id}
    ]
    obj = task_links.index(ctx.repo)[item.task_id]
    assert [(m.session_id, m.source, m.idx) for m in obj.mentions] == [
        (new.id, "notes", -1),
        (old.id, "summary", 0),
    ]
    assert task_links.object_of(ctx.repo, new.id, item.id).key == item.task_id


def test_a_linked_summary_mention_tick_reaches_the_notes_line(ctx, sidecar):
    a = _notes_meeting(ctx, "Infra weekly", 1, "update the docs")
    (item,) = ctx.repo.notes_items(a.id)
    b = _meeting(ctx, "Infra weekly 2", [_item("Update the docs", "Ada Lovelace", "b1")], 8)
    assert [lk.other_item_id for lk in task_links.link_items(ctx, b.id)] == [item.id]
    tid = _items(ctx, b.id)[0].task_id
    assert ctx.repo.notes_items(a.id)[0].task_id == tid
    before = ctx.repo.sync_state(a.id)[1]
    assert task_links.set_done(ctx, tid, True, by="Bob") == 2
    assert ctx.repo.notes_items(a.id)[0].done
    assert ctx.repo.get(a.id).notes.startswith("- [x]")
    assert ctx.repo.notes_ops(a.id)[-1]["frame"].startswith(b"\xde")
    assert ctx.repo.sync_state(a.id)[1] != before
    assert _history(ctx, a.id, "task_done") == [{"task": "update the docs", "by": "Bob"}]
    # follow and an issue link reach it too, without touching the document.
    n = len(ctx.repo.notes_ops(a.id))
    task_links.set_follow(ctx, tid, True)
    task_links.set_issue_url(ctx, tid, "https://x/1")
    stored = ctx.repo.notes_items(a.id)[0]
    assert (stored.follow, stored.issue_url) == (True, "https://x/1")
    assert len(ctx.repo.notes_ops(a.id)) == n


def test_reconcile_ticks_a_notes_mention_from_its_head(ctx, sidecar):
    tid = "abcabcabcabc"
    a = _notes_meeting(ctx, "Infra weekly", 1, "update the docs")
    (item,) = ctx.repo.notes_items(a.id)
    assert notes_items.write_fields(ctx, a.id, item.id, task_id=tid)
    _meeting(ctx, "Later", [_item("Update the docs", "Ada", "b1", task_id=tid, done=True)], 8)
    assert task_links.reconcile(ctx) == 1
    assert ctx.repo.notes_items(a.id)[0].done
    assert ctx.repo.get(a.id).notes.startswith("- [x]")
    assert task_links.reconcile(ctx) == 0


def test_a_notes_mention_the_notes_cannot_take_fails_the_tick(ctx, sidecar, monkeypatch):
    a = _notes_meeting(ctx, "Infra weekly", 1, "update the docs")
    (item,) = ctx.repo.notes_items(a.id)
    monkeypatch.setenv("FAKE_NOTES_DOC_FAIL", "1")
    with pytest.raises(shared_notes.NotesDocError):
        task_links.set_done(ctx, f"{a.id}:{item.id}", True)
    assert not ctx.repo.notes_items(a.id)[0].done


def test_continued_notes_covers_notes_items(ctx, sidecar):
    from mnemosyne.api.routes.export import continued_notes

    old = _notes_meeting(ctx, "Planning", 1, "send the deck")
    later = _meeting(ctx, "Follow-up", [_item("Send the deck", "Ada Lovelace", "b1")], 8)
    task_links.link_items(ctx, later.id, "exact")
    (item,) = ctx.repo.notes_items(old.id)
    assert item.task_id
    assert continued_notes(ctx, ctx.repo.get(old.id)) == {item.id: "2026-10-08-Follow-up"}
    assert continued_notes(ctx, ctx.repo.get(later.id)) == {}  # the head is not continued
