# ruff: noqa: F811  (the fixtures imported from test_share_pull are used by name)
"""The recipient's outbox (services/share_push.py, task 16): what I do on a meeting someone shared
with me (comments, ticks, typed notes) is signed and sent to its owner's computer, which applies
it as me. Owner and recipient apps in this process, talking through `httpx.ASGITransport` as in
test_share_pull.py; notes through the stand-in sidecar."""

import base64

import httpx
import pytest

from mnemosyne.models.session import ActionItem, Session, SummaryData
from mnemosyne.services import share_push, shared_notes
from tests.fake_link import doc_frame
from tests.test_share_pull import (  # noqa: F401 - fixtures
    ENDPOINTS,
    Node,
    befriend,
    pair,
    pull,
    share,
    sidecar,
    to_app,
)

pytestmark = pytest.mark.anyio

ITEM = "a1b2c3d4e5f6"


def meeting(node: Node, notes: str = "Scratch.\n") -> str:
    from datetime import datetime

    s = Session(
        name="Release sync",
        created_at=datetime(2026, 10, 20, 10, 0),
        summary="We ship on Friday.",
        summary_data=SummaryData(action_items=[ActionItem(id=ITEM, text="Send the release notes")]),
        notes=notes,
    )
    return node.repo.save(s).id


async def arrive(owner: Node, recipient: Node, notes: str = "Scratch.\n", **parts) -> str:
    sid = meeting(owner, notes)
    share(owner, recipient, sid, **parts)
    await pull(owner, recipient)
    assert recipient.repo.shared_from(sid) == owner.person
    return sid


async def comment(node: Node, sid: str, text="Spring works", **extra) -> dict:
    async with node.admin() as c:
        r = await c.post(f"/api/sessions/{sid}/comments", json={"text": text, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def status(node: Node, sid: str) -> dict:
    async with node.admin() as c:
        r = await c.get(f"/api/sessions/{sid}/share-status")
    assert r.status_code == 200, r.text
    return r.json()


async def comments_of(node: Node, sid: str) -> dict:
    async with node.admin() as c:
        return (await c.get(f"/api/sessions/{sid}/comments")).json()


async def send(owner: Node, recipient: Node):
    row = recipient.repo.contact(owner.person)
    return await share_push.push(recipient.ctx, row)


def owner_comments(owner: Node, sid: str):
    return [c for c in owner.repo.list_comments(sid)]


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def ops_from(owner: Node, recipient: Node, sid: str) -> list[dict]:
    """The owner's notes log entries typed by the recipient."""
    pid = recipient.ctx.person.pid
    return [o for o in owner.repo.notes_ops(sid) if o["author_id"] == pid]


def type_notes(node: Node, sid: str, *raw: bytes) -> None:
    """Frames typed on this computer, as its notes socket logs them."""
    shared_notes.accept_frames(
        node.ctx, sid, list(raw), node.ctx.person.pid, "Recipient", can_edit=True
    )


# ---- comments ---------------------------------------------------------------------------


async def test_a_comment_reaches_the_owner_as_me_and_comes_back_once(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    mine = await comment(recipient, sid)
    assert mine["author"] == "Recipient" and mine["author_id"] == recipient.ctx.person.pid
    assert (await status(recipient, sid))["waiting"] == {"comments": 1, "ticks": 0, "notes": False}
    parts = (await status(recipient, sid))["parts"]
    assert parts["summary"] is True and parts["transcript"] is False
    assert owner_comments(owner, sid) == []

    summary = await send(owner, recipient)
    assert (summary.state, summary.sent, summary.accepted, summary.rejected) == ("ok", 1, 1, 0)
    [theirs] = owner_comments(owner, sid)
    assert theirs.id == mine["id"] and theirs.text == "Spring works"
    assert theirs.author == "Recipient" and theirs.author_id == recipient.ctx.person.pid
    st = await status(recipient, sid)
    assert st["waiting"]["comments"] == 0 and st["rejected"] == []
    assert st["owner"] == {"person": owner.person, "name": "Owner"}

    # The next pull replaces comments whole with the owner's: mine is there once, same id.
    await pull(owner, recipient)
    [back] = recipient.repo.list_comments(sid)
    assert back.id == mine["id"] and back.text == "Spring works"
    # Nothing to send any more: not even a hello.
    seen: list = []
    recipient.puller.client_for = to_app(owner.app, seen)
    assert (await send(owner, recipient)).state == "nothing" and seen == []


async def test_edit_resolve_reopen_and_delete_follow_in_order(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    c = await comment(recipient, sid)
    async with recipient.admin() as http:
        base = f"/api/sessions/{sid}/comments/{c['id']}"
        assert (await http.patch(base, json={"text": "Spring, then"})).status_code == 200
        assert (await http.post(base + "/resolve")).status_code == 200
        assert (await http.post(base + "/reopen")).status_code == 200
        assert (await http.post(base + "/resolve")).status_code == 200
    assert (await send(owner, recipient)).accepted == 5
    [theirs] = owner_comments(owner, sid)
    assert theirs.text == "Spring, then" and theirs.resolved_at is not None
    async with recipient.admin() as http:
        assert (await http.delete(f"/api/sessions/{sid}/comments/{c['id']}")).status_code == 200
    assert (await send(owner, recipient)).accepted == 1
    assert owner_comments(owner, sid) == []


async def test_a_reply_to_the_owners_thread_goes_back(pair):
    owner, recipient = pair
    sid = meeting(owner)
    share(owner, recipient, sid)
    root = owner.repo.add_comment(sid, text="Question?", author_id="", author="Owner")
    await pull(owner, recipient)
    reply = await comment(recipient, sid, "Answer.", parent_id=root)
    assert (await send(owner, recipient)).accepted == 1
    assert [c.id for c in owner_comments(owner, sid)] == [root, reply["id"]]
    assert owner.repo.get_comment(reply["id"]).parent_id == root


# ---- ticks ------------------------------------------------------------------------------


async def test_a_tick_reaches_the_owner(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    async with recipient.admin() as http:
        r = await http.patch(f"/api/sessions/{sid}/action-items/0", json={"done": True})
    assert r.status_code == 200, r.text
    assert recipient.repo.get(sid).summary_data.action_items[0].done
    assert (await status(recipient, sid))["waiting"]["ticks"] == 1
    assert (await send(owner, recipient)).accepted == 1
    assert owner.repo.get(sid).summary_data.action_items[0].done
    await pull(owner, recipient)
    assert recipient.repo.get(sid).summary_data.action_items[0].done
    assert (await status(recipient, sid))["waiting"]["ticks"] == 0


# ---- refused ----------------------------------------------------------------------------


async def test_what_the_owner_refuses_is_marked_with_its_reason(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    # The owner takes the action items and the summary back; the recipient has not pulled yet.
    share(owner, recipient, sid, action_items=False, summary=False)
    async with recipient.admin() as http:
        r = await http.patch(f"/api/sessions/{sid}/action-items/0", json={"done": True})
        assert r.status_code == 200, r.text
    await comment(recipient, sid, "A thought on the summary " * 6, target="body", exact="We ship")
    summary = await send(owner, recipient)
    assert (summary.accepted, summary.rejected) == (0, 2)
    assert not owner.repo.get(sid).summary_data.action_items[0].done
    st = await status(recipient, sid)
    assert st["waiting"] == {"comments": 0, "ticks": 0, "notes": False}
    by_kind = {r["kind"]: r for r in st["rejected"]}
    assert by_kind["item_flag"]["reason"] == "This meeting's action items are not shared with you"
    assert by_kind["item_flag"]["excerpt"] == "Send the release notes"
    assert by_kind["comment"]["reason"] == "This meeting's summary is not shared with you"
    assert len(by_kind["comment"]["excerpt"]) == 80 and by_kind["comment"]["at"]
    # Refused is final: it is not sent again.
    assert (await send(owner, recipient)).state == "nothing"


# ---- unreachable owner, resends ----------------------------------------------------------


async def test_an_offline_owner_leaves_everything_pending_for_the_next_round(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    online = recipient.puller.client_for

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    recipient.puller.client_for = lambda listen, token: httpx.AsyncClient(
        transport=httpx.MockTransport(down), base_url=f"http://{listen}"
    )
    await comment(recipient, sid)
    summary = await send(owner, recipient)
    assert summary.state == "unreachable" and summary.accepted == 0
    assert (await status(recipient, sid))["waiting"]["comments"] == 1
    assert owner_comments(owner, sid) == []

    recipient.puller.client_for = online
    st = await pull(owner, recipient)  # a round pushes first
    assert st.state == "ok"
    assert len(owner_comments(owner, sid)) == 1
    assert (await status(recipient, sid))["waiting"]["comments"] == 0
    assert len(recipient.repo.list_comments(sid)) == 1


class LosesTheAnswer(httpx.AsyncBaseTransport):
    """The owner takes the contributions, the answer never arrives."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.lose = True

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if self.lose and request.url.path == "/api/share/contributions":
            await response.aread()
            raise httpx.ReadError("answer lost", request=request)
        return response


async def test_a_resend_after_a_lost_answer_applies_nothing_twice(pair, sidecar):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    transport = LosesTheAnswer(owner.app)
    recipient.puller.client_for = lambda listen, token: httpx.AsyncClient(
        transport=transport,
        base_url=f"http://{listen}",
        headers={"Authorization": f"Bearer {token}"},
    )
    await comment(recipient, sid)
    type_notes(recipient, sid, frame("f1", ["ins", 0, "Typed. "]))
    assert (await send(owner, recipient)).state == "unreachable"
    assert len(owner_comments(owner, sid)) == 1  # it did arrive
    assert len(ops_from(owner, recipient, sid)) == 1
    assert (
        recipient.repo.pushed_seq(sid) == 0 and (await status(recipient, sid))["waiting"]["notes"]
    )

    transport.lose = False
    summary = await send(owner, recipient)
    assert (summary.state, summary.accepted) == ("ok", 2)
    assert len(owner_comments(owner, sid)) == 1
    assert len(ops_from(owner, recipient, sid)) == 1
    assert not (await status(recipient, sid))["waiting"]["notes"]


# ---- typed notes -------------------------------------------------------------------------


async def test_typed_notes_reach_the_owner_and_the_cursor_moves(pair, sidecar):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    assert recipient.repo.pushed_seq(sid) == 0
    type_notes(recipient, sid, frame("f1", ["ins", 0, "Typed by the recipient\n"]))
    assert (await status(recipient, sid))["waiting"]["notes"] is True

    summary = await send(owner, recipient)
    assert (summary.sent, summary.accepted) == (1, 1)
    [op] = ops_from(owner, recipient, sid)
    assert op["author_id"] == recipient.ctx.person.pid and op["author"] == "Recipient"
    top = max(o["seq"] for o in recipient.repo.notes_ops(sid) if o["author_id"])
    assert recipient.repo.pushed_seq(sid) == top
    assert (await status(recipient, sid))["waiting"]["notes"] is False
    shared_notes.fold_blocking(owner.ctx, sid)
    assert owner.repo.get(sid).notes == "Typed by the recipient\nScratch.\n"

    # The pull brings the owner's document back; what it holds of mine is not sent again.
    await pull(owner, recipient)
    assert recipient.repo.get(sid).notes == "Typed by the recipient\nScratch.\n"
    seen: list = []
    recipient.puller.client_for = to_app(owner.app, seen)
    assert (await send(owner, recipient)).state == "nothing" and seen == []
    assert len(ops_from(owner, recipient, sid)) == 1


async def test_many_frames_go_in_chunks_and_each_cursor_step_needs_an_accept(
    pair, sidecar, monkeypatch
):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    monkeypatch.setattr(share_push, "CHUNK_FRAMES", 2)
    for i in range(5):
        type_notes(recipient, sid, frame(f"f{i}", ["ins", 0, f"{i}"]))
    seen: list = []
    recipient.puller.client_for = to_app(owner.app, seen)
    summary = await send(owner, recipient)
    assert (summary.sent, summary.accepted) == (3, 3)  # 2 + 2 + 1 frames
    assert len(ops_from(owner, recipient, sid)) == 5
    assert len([r for r in seen if r.url.path == "/api/share/contributions"]) == 1  # one batch


async def test_notes_the_owner_refuses_keep_the_cursor_and_say_why(pair, sidecar):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    type_notes(recipient, sid, frame("f1", ["ins", 0, "Typed\n"]))
    share(owner, recipient, sid, notes=False)  # before the recipient hears of it
    summary = await send(owner, recipient)
    assert (summary.accepted, summary.rejected) == (0, 1)
    assert recipient.repo.pushed_seq(sid) == 0
    [r] = (await status(recipient, sid))["rejected"]
    assert r["kind"] == "notes" and r["reason"] == "This meeting's notes are not shared with you"
    # Shared again: the same frames go through and the reason is gone.
    share(owner, recipient, sid, notes=True)
    assert (await send(owner, recipient)).accepted == 1
    assert (await status(recipient, sid))["rejected"] == []


# ---- a pull in between -------------------------------------------------------------------


async def test_pending_work_survives_a_pull_that_replaces_the_copy(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    kept = await comment(recipient, sid, "Will be edited")
    gone = await comment(recipient, sid, "Will be deleted")
    async with recipient.admin() as http:
        assert (
            await http.patch(f"/api/sessions/{sid}/comments/{kept['id']}", json={"text": "Edited"})
        ).status_code == 200
        assert (await http.delete(f"/api/sessions/{sid}/comments/{gone['id']}")).status_code == 200
        r = await http.patch(f"/api/sessions/{sid}/action-items/0", json={"done": True})
        assert r.status_code == 200
    owner.repo.update_fields(
        sid, summary="We ship on Monday."
    )  # a new digest: the copy is replaced

    # The owner's side refuses contributions for now (503): they stay pending through the pull.
    real = recipient.puller.client_for

    class Refusing(httpx.AsyncBaseTransport):
        inner = httpx.ASGITransport(app=owner.app)

        async def handle_async_request(self, request):
            if request.url.path == "/api/share/contributions":
                return httpx.Response(503, request=request)
            return await self.inner.handle_async_request(request)

    recipient.puller.client_for = lambda listen, token: httpx.AsyncClient(
        transport=Refusing(),
        base_url=f"http://{listen}",
        headers={"Authorization": f"Bearer {token}"},
    )
    st = await pull(owner, recipient)
    assert st.state == "ok" and recipient.repo.get(sid).summary == "We ship on Monday."
    shown = {c.id: c for c in recipient.repo.list_comments(sid)}
    assert shown[kept["id"]].text == "Edited" and shown[kept["id"]].author == "Recipient"
    assert gone["id"] not in shown  # not brought back
    assert recipient.repo.get(sid).summary_data.action_items[0].done  # nor the tick undone

    recipient.puller.client_for = real
    st = await pull(owner, recipient)
    assert st.state == "ok"
    [theirs] = owner_comments(owner, sid)
    assert theirs.id == kept["id"] and theirs.text == "Edited"
    [back] = recipient.repo.list_comments(sid)
    assert back.id == kept["id"]
    assert (await status(recipient, sid))["waiting"] == {"comments": 0, "ticks": 0, "notes": False}


# ---- when it is sent ---------------------------------------------------------------------


async def test_a_local_write_schedules_one_debounced_push(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    pusher = recipient.ctx.share_push
    pusher.debounce = 0.05
    pusher.start()
    seen: list = []
    recipient.puller.client_for = to_app(owner.app, seen)
    try:
        await comment(recipient, sid, "One")
        await comment(recipient, sid, "Two")
        await pusher.drain()
        await recipient.puller.drain()
    finally:
        await pusher.stop()
    assert sorted(c.text for c in owner_comments(owner, sid)) == ["One", "Two"]
    posts = [r for r in seen if r.url.path == "/api/share/contributions"]
    assert len(posts) == 1  # both writes in one batch
    assert (await status(recipient, sid))["waiting"]["comments"] == 0


async def test_events_say_how_much_is_waiting(pair):
    owner, recipient = pair
    sid = await arrive(owner, recipient)
    events: list = []
    recipient.ctx.bus.publish = lambda e, _real=recipient.ctx.bus.publish: (
        events.append(e) or _real(e)
    )
    await comment(recipient, sid)
    assert {"type": "share_outbox", "session_id": sid, "pending": 1} in events
    await send(owner, recipient)
    assert events[-1] == {"type": "share_outbox", "session_id": sid, "pending": 0}


# ---- the status route --------------------------------------------------------------------


async def test_status_is_404_for_a_meeting_that_is_not_shared_with_me(pair):
    owner, recipient = pair
    mine = meeting(recipient)
    async with recipient.admin() as http:
        assert (await http.get(f"/api/sessions/{mine}/share-status")).status_code == 404
        assert (await http.get("/api/sessions/nope/share-status")).status_code == 404


async def test_writes_on_my_own_meetings_queue_nothing(pair):
    owner, recipient = pair
    mine = meeting(recipient)
    await comment(recipient, mine)
    assert recipient.repo.outbox_pending() == []


# ---- three computers ---------------------------------------------------------------------


@pytest.fixture
async def trio(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_CONFIG_FILE", str(tmp_path / "config.toml"))
    ENDPOINTS.setdefault("Bea", "c3" * 32)
    ana, owner, bea = Node(tmp_path, "Recipient"), Node(tmp_path, "Owner"), Node(tmp_path, "Bea")
    befriend(owner, ana)
    befriend(owner, bea)
    yield ana, owner, bea
    for n in (ana, owner, bea):
        await n.puller.stop()
        await n.ctx.jobs.shutdown()
        n.repo.close()


async def test_a_comment_and_typed_notes_go_through_the_owner_to_the_third(trio, sidecar):
    ana, owner, bea = trio
    sid = meeting(owner)
    share(owner, ana, sid)
    share(owner, bea, sid)
    await pull(owner, ana)
    await pull(owner, bea)

    mine = await comment(ana, sid, "From Ana")
    type_notes(ana, sid, frame("f1", ["ins", 0, "Ana was here\n"]))
    summary = await send(owner, ana)
    assert (summary.accepted, summary.rejected) == (2, 0)
    shared_notes.fold_blocking(owner.ctx, sid)

    await pull(owner, bea)
    [seen_by_bea] = bea.repo.list_comments(sid)
    assert seen_by_bea.id == mine["id"] and seen_by_bea.text == "From Ana"
    assert seen_by_bea.author == "Recipient" and seen_by_bea.author_id == ana.ctx.person.pid
    assert bea.repo.get(sid).notes == "Ana was here\nScratch.\n"
    # Bea never sends back what she was given.
    assert bea.repo.outbox_pending() == []
    assert bea.repo.notes_ops_after(sid, 0, bea.ctx.person.pid) == []
    assert (await send(owner, bea)).state == "nothing"


# ---- a notes comment is marked once, by its author's own server ---------------------------


def marks_in(node: Node, sid: str, comment_id: str) -> int:
    """How many logged frames of the notes put the mark of this comment."""
    return sum(
        1 for o in node.repo.notes_ops(sid) if f'"comment", "{comment_id}"'.encode() in o["frame"]
    )


async def test_a_notes_comment_is_marked_once_on_both_computers(pair, sidecar):
    owner, recipient = pair
    sid = meeting(owner, notes="")
    type_notes(owner, sid, frame("f1", ["ins", 0, "Our launch plan is set\n"]))
    shared_notes.fold_blocking(owner.ctx, sid)
    share(owner, recipient, sid)
    await pull(owner, recipient)
    assert recipient.repo.get(sid).notes == "Our launch plan is set\n"

    # The toy "element ids" are positions: "launch plan" is characters 4..14.
    c = await comment(
        recipient, sid, "Why?", target="notes", exact="launch plan", notes_ids=["4", "14"]
    )
    assert c["notes_range"] == [4, 15]
    assert (await send(owner, recipient)).accepted == 2  # the comment, then the mark's frame
    shared_notes.fold_blocking(owner.ctx, sid)

    # Only the recipient's server ran the sidecar's mark_comment; the hub took the frame.
    assert [x["op"] for x in sidecar.doc_requests].count("mark_comment") == 1
    assert owner.repo.notes_marks(sid)[1] == {c["id"]: [4, 15]}
    assert marks_in(owner, sid, c["id"]) == 1
    [thread] = (await comments_of(owner, sid))["threads"]
    assert thread["root"]["notes_range"] == [4, 15] and not thread["root"]["detached"]

    await pull(owner, recipient)
    assert recipient.repo.notes_marks(sid)[1] == {c["id"]: [4, 15]}
    assert marks_in(recipient, sid, c["id"]) == 1
    # And nothing of it is sent a second time.
    assert (await send(owner, recipient)).state == "nothing"
