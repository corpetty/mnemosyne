"""Comments, storage side: CRUD, cascade, deleted-with-replies, combine, sync bumps, access."""

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mnemosyne import access
from mnemosyne.api.app import create_app
from mnemosyne.models.comment import Comment
from mnemosyne.models.session import Session
from mnemosyne.services import sharing
from mnemosyne.storage.sqlite import SessionRepository
from tests.test_combine import _meeting as _audio_meeting
from tests.test_combine import _wait


@pytest.fixture
def repo(tmp_path):
    r = SessionRepository(tmp_path / "db.sqlite")
    r.device_id = "dev-a"
    yield r
    r.close()


def _session(repo, name="One") -> str:
    return repo.save(Session(name=name)).id


def _state(repo, sid):
    seq = repo._conn.execute("SELECT sync_seq FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    _, vv = repo.sync_state(sid)
    return seq, vv.get("dev-a", 0)


def test_add_get_list_roundtrip(repo):
    sid = _session(repo)
    cid = repo.add_comment(
        sid,
        target="segment",
        anchor={"part": 1, "start": 2.5, "end": 4.0},
        quote="the plan",
        text="Is this right?",
        author_id="u1",
        author="Ann",
        mentions=["u2"],
    )
    assert len(cid) == 12 and int(cid, 16) >= 0
    reply = repo.add_comment(sid, parent_id=cid, text="Yes", author="Bob")
    c = repo.get_comment(cid)
    assert isinstance(c, Comment)
    assert (c.session_id, c.parent_id, c.target, c.target_id) == (sid, "", "segment", "")
    assert c.anchor == {"part": 1, "start": 2.5, "end": 4.0}
    assert (c.quote, c.text, c.author_id, c.author) == ("the plan", "Is this right?", "u1", "Ann")
    assert c.mentions == ["u2"] and c.resolved_at is None and c.resolved_by == ""
    assert c.deleted is False and c.created_at == c.updated_at
    assert [x.id for x in repo.list_comments(sid)] == [cid, reply]
    assert repo.get_comment("nope") is None
    assert repo.list_comments("nope") == []
    rows = repo.comment_rows(sid)
    assert [r["id"] for r in rows] == [cid, reply]
    assert rows[0]["anchor"]["part"] == 1 and rows[0]["mentions"] == ["u2"]
    assert rows[0]["deleted"] is False


def test_add_rejects_unknown_fields_and_missing_text(repo):
    sid = _session(repo)
    with pytest.raises(TypeError):
        repo.add_comment(sid, text="x", colour="red")
    with pytest.raises(TypeError):
        repo.add_comment(sid, author="Ann")
    assert repo.list_comments(sid) == []


def test_edit_resolve_reopen(repo):
    sid = _session(repo)
    cid = repo.add_comment(sid, text="first", mentions=["u1"])
    before = repo.get_comment(cid).updated_at
    assert repo.update_comment_text(cid, "second")
    c = repo.get_comment(cid)
    assert c.text == "second" and c.mentions == ["u1"] and c.updated_at >= before
    assert repo.update_comment_text(cid, "third", mentions=[])
    assert repo.get_comment(cid).mentions == []

    when = datetime(2026, 10, 21, 9, 30)
    assert repo.set_comment_resolved(cid, when, "u2")
    c = repo.get_comment(cid)
    assert c.resolved_at == when and c.resolved_by == "u2"
    assert repo.set_comment_resolved(cid, None, "u2")  # reopened
    c = repo.get_comment(cid)
    assert c.resolved_at is None and c.resolved_by == ""

    assert not repo.update_comment_text("nope", "x")
    assert not repo.set_comment_resolved("nope", when, "u2")
    assert not repo.delete_comment("nope")


def test_delete_without_replies_removes_the_row(repo):
    sid = _session(repo)
    root = repo.add_comment(sid, text="root")
    reply = repo.add_comment(sid, parent_id=root, text="reply")
    assert repo.delete_comment(reply)
    assert [c.id for c in repo.list_comments(sid)] == [root]
    assert repo.delete_comment(root)
    assert repo.list_comments(sid) == []
    assert not repo.delete_comment(root)


def test_delete_with_replies_keeps_a_blanked_row(repo):
    sid = _session(repo)
    root = repo.add_comment(sid, text="secret", mentions=["u1"], quote="q", target="body")
    reply = repo.add_comment(sid, parent_id=root, text="answer")
    assert repo.delete_comment(root)
    c = repo.get_comment(root)
    assert c.deleted is True and c.text == "" and c.mentions == []
    assert c.quote == "q" and c.target == "body"  # still anchored, for the thread
    assert [x.id for x in repo.list_comments(sid)] == [root, reply]
    assert repo.get_comment(reply).text == "answer"
    # Already deleted: nothing to do, and it can no longer be edited.
    seq = _state(repo, sid)
    assert not repo.delete_comment(root)
    assert not repo.update_comment_text(root, "back")
    assert _state(repo, sid) == seq
    # Once the last reply goes the blanked root can be removed for good.
    assert repo.delete_comment(reply)
    assert repo.delete_comment(root) is True
    assert repo.get_comment(root) is None


def test_comments_go_with_their_meeting(repo):
    a, b = _session(repo, "A"), _session(repo, "B")
    repo.add_comment(a, text="on a")
    keep = repo.add_comment(b, text="on b")
    assert repo.delete(a)
    n = repo._conn.execute("SELECT count(*) FROM comments").fetchone()[0]
    assert n == 1 and [c.id for c in repo.list_comments(b)] == [keep]


def test_each_write_bumps_the_version_once(repo):
    a, b = _session(repo, "A"), _session(repo, "B")
    root = repo.add_comment(a, text="root")  # (setup, bumps already)
    seq = repo.current_sync_seq()

    def check(act, expect=1):
        nonlocal seq
        before = _state(repo, a), _state(repo, b)
        act()
        after = _state(repo, a), _state(repo, b)
        assert after[0][1] == before[0][1] + expect
        assert after[0][0] > before[0][0]
        assert after[1] == before[1]  # the other meeting is left alone
        assert repo.current_sync_seq() == seq + expect
        seq += expect

    check(lambda: repo.add_comment(a, text="x"))
    check(lambda: repo.update_comment_text(root, "y"))
    check(lambda: repo.set_comment_resolved(root, datetime.now(), "u"))
    check(lambda: repo.set_comment_resolved(root, None))
    reply = repo.add_comment(a, parent_id=root, text="r")
    seq = repo.current_sync_seq()
    check(lambda: repo.delete_comment(reply))  # hard delete
    reply = repo.add_comment(a, parent_id=root, text="r")
    seq = repo.current_sync_seq()
    check(lambda: repo.delete_comment(root))  # blanked
    # Writes that find nothing leave no trace.
    before = _state(repo, a), repo.current_sync_seq()
    repo.update_comment_text("nope", "z")
    repo.delete_comment("nope")
    repo.set_comment_resolved("nope", None)
    assert (_state(repo, a), repo.current_sync_seq()) == before
    # Reads do not bump.
    repo.list_comments(a)
    repo.get_comment(root)
    assert (_state(repo, a), repo.current_sync_seq()) == before
    assert reply


def test_a_failed_write_leaves_no_trace(repo):
    before = repo.current_sync_seq()
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_comment("no-such-meeting", text="x")  # foreign key
    assert repo.current_sync_seq() == before
    assert repo._conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 0


def test_move_comments_shifts_parts_and_follows_the_meeting(repo):
    target, other = _session(repo, "target"), _session(repo, "other")
    t0 = repo.add_comment(
        target, target="segment", anchor={"part": 0, "start": 1, "end": 2}, text="t"
    )
    t_body = repo.add_comment(target, target="body", anchor={"exact": "words"}, text="t")
    o0 = repo.add_comment(
        other, target="segment", anchor={"part": 0, "start": 3, "end": 3}, text="t"
    )
    o1 = repo.add_comment(
        other, target="segment", anchor={"part": 1, "start": 5, "end": 6}, text="t"
    )
    o_body = repo.add_comment(other, target="body", anchor={"exact": "other words"}, text="t")
    o_item = repo.add_comment(other, target="item", target_id="a1", text="t")
    o_lost = repo.add_comment(
        other, target="segment", quote="gone", anchor={"part": 7, "start": 1, "end": 2}, text="t"
    )
    untouched = _session(repo, "elsewhere")
    e = repo.add_comment(untouched, anchor={"part": 0, "start": 0, "end": 1}, text="t")

    # The other meeting's recording came first: its parts 0 and 1 became 0 and 1, this
    # meeting's part 0 became 2 (the same shape services/combine.py passes).
    repo.move_comments([(other, 0, target, 0), (other, 1, target, 1), (target, 0, target, 2)])
    got = {c.id: c for c in repo.list_comments(target)}
    assert set(got) == {t0, t_body, o0, o1, o_body, o_item, o_lost}
    assert got[t0].anchor == {"part": 2, "start": 1, "end": 2}
    assert got[o0].anchor == {"part": 0, "start": 3, "end": 3}
    assert got[o1].anchor == {"part": 1, "start": 5, "end": 6}
    assert got[t_body].anchor == {"exact": "words"}
    assert got[o_body].anchor == {"exact": "other words"} and got[o_item].target_id == "a1"
    assert got[o_lost].anchor == {} and got[o_lost].quote == "gone"  # kept, without a place
    assert repo.list_comments(other) == []
    assert repo.get_comment(e).anchor == {"part": 0, "start": 0, "end": 1}
    assert repo.list_comments(untouched)[0].session_id == untouched


def test_move_comments_bumps_both_meetings_once_and_only_when_something_moved(repo):
    a, b = _session(repo, "A"), _session(repo, "B")
    other = _session(repo, "C")
    repo.add_comment(a, anchor={"part": 0, "start": 0, "end": 1}, text="t")
    before = {s: _state(repo, s) for s in (a, b, other)}
    seq = repo.current_sync_seq()
    repo.move_comments([(other, 0, b, 0)])  # nothing of `other` to move
    assert {s: _state(repo, s) for s in (a, b, other)} == before
    assert repo.current_sync_seq() == seq
    repo.move_comments([(a, 0, b, 1)])
    for s in (a, b):
        assert _state(repo, s)[1] == before[s][1] + 1
    assert _state(repo, other) == before[other]
    assert repo.current_sync_seq() == seq + 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_combining_meetings_carries_their_comments(client, ctx):
    t0 = datetime(2026, 9, 28, 10, 0)
    later = _audio_meeting(
        ctx, "Planning", 3.0, t0 + timedelta(minutes=10), [("Ana", 0.5, "hello again")], {}
    )
    earlier = _audio_meeting(
        ctx, "Dropped", 2.0, t0, [("Bob", 0.2, "can you hear me")], {}
    )  # fmt: skip
    mine = ctx.repo.add_comment(
        later, target="segment", anchor={"part": 0, "start": 0.5, "end": 0.9}, text="mine"
    )
    theirs = ctx.repo.add_comment(
        earlier, target="segment", anchor={"part": 0, "start": 0.2, "end": 0.6}, text="theirs"
    )
    summary = ctx.repo.add_comment(earlier, target="body", anchor={"exact": "x"}, text="body")

    job = client.post(f"/api/sessions/{later}/combine", json={"other_id": earlier}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "completed", done["error"]

    got = {c.id: c for c in ctx.repo.list_comments(later)}
    assert set(got) == {mine, theirs, summary}
    assert got[theirs].anchor["part"] == 0  # the earlier recording is part 0 now
    assert got[mine].anchor["part"] == 1  # this meeting's moved to part 1
    assert got[summary].anchor == {"exact": "x"}
    assert ctx.repo.list_comments(earlier) == []


# ---- team server -----------------------------------------------------------


@pytest.fixture
def team(settings, keystore):
    settings.team_mode = True
    app = create_app(settings, keystore=keystore)
    ctx = app.state.ctx
    people = {}
    for name, role in [("Admin", "admin"), ("Ann", "member"), ("Bob", "member"), ("Cat", "member")]:
        people[name] = ctx.users.add(name, f"{name.lower()}@team.test", role)
    with TestClient(app):
        yield ctx, people


@contextmanager
def _as(user):
    reset = access.current.set(access.Principal(user.id, user.name, user.role))
    try:
        yield
    finally:
        access.current.reset(reset)


def test_a_reader_can_comment_and_a_stranger_cannot_see_the_meeting(team):
    ctx, people = team
    ann, bob, cat = people["Ann"], people["Bob"], people["Cat"]
    with _as(ann):
        sid = ctx.repo.save(Session(name="Planning")).id
        mine = ctx.repo.add_comment(sid, text="note to self", author_id=ann.id, author="Ann")
    sharing.set_shares(ctx, sid, {bob.id})

    with _as(bob):  # shared with him: reads, comments, but the meeting itself is read-only
        reply = ctx.repo.add_comment(sid, parent_id=mine, text="seen", author_id=bob.id)
        assert [c.id for c in ctx.repo.list_comments(sid)] == [mine, reply]
        assert ctx.repo.get_comment(reply).author_id == bob.id
        assert ctx.repo.set_comment_resolved(mine, datetime.now(), bob.id)
        assert ctx.repo.update_comment_text(reply, "seen it")
        assert ctx.repo.delete_comment(reply)
        with pytest.raises(access.Forbidden):
            ctx.repo.update_fields(sid, name="mine now")

    with _as(cat):  # a stranger: the meeting and its comments are not there for her
        with pytest.raises(access.Hidden):
            ctx.repo.list_comments(sid)
        with pytest.raises(access.Hidden):
            ctx.repo.add_comment(sid, text="hi")
        with pytest.raises(access.Hidden):
            ctx.repo.get_comment(mine)
        with pytest.raises(access.Hidden):
            ctx.repo.update_comment_text(mine, "x")
        with pytest.raises(access.Hidden):
            ctx.repo.set_comment_resolved(mine, None)
        with pytest.raises(access.Hidden):
            ctx.repo.delete_comment(mine)
        assert ctx.repo.get_comment("nope") is None  # a missing id reveals nothing either

    with _as(ann):
        assert ctx.repo.get_comment(mine).text == "note to self"
        assert ctx.repo.get_comment(mine).resolved_by == bob.id
