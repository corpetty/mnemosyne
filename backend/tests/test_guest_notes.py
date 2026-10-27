"""Guest notes (services/guest_notes.py): the owner's bridge between a meeting's live notes and a
room on a publish host. The host is tests/fake_guest_room.py (HTTP through `ctx.http_transport`,
the WebSocket on 127.0.0.1); the sidecar is the toy `notes doc` of tests/fake_link.py."""

import asyncio
import base64
import json
import time

import httpx
import pytest

from mnemosyne.models.session import AgendaItem, Session
from mnemosyne.services import guest_format as gf
from mnemosyne.services import guest_notes, shared_notes
from mnemosyne.services import publication_format as pf
from tests import fake_link
from tests.fake_guest_room import T_NOTES, T_PRESENCE, FakeRoomHost, GuestClient, routed
from tests.fake_link import doc_frame

pytestmark = pytest.mark.anyio


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    return fake_link.install(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """No timers in the middle of a test: housekeeping only when a test calls `tick`."""
    monkeypatch.setattr(shared_notes, "FOLD_DELAY", 1000.0)
    monkeypatch.setattr(shared_notes, "FOLD_EVERY", 1000.0)
    monkeypatch.setattr(guest_notes, "TICK", 1000.0)
    monkeypatch.setattr(guest_notes, "BACKOFF", (0.01,))
    monkeypatch.setattr(guest_notes, "RETRY_DELAY", 0.0)


@pytest.fixture
async def host(ctx, settings, sidecar):
    h = await FakeRoomHost().start()
    ctx.http_transport = h.transport()
    settings.publish_host = h.url
    settings.guest_transport = "host"  # phase 9's rooms; iroh rooms are further down
    settings.local_speaker_name = "Corey"
    yield h
    await guest_notes.shutdown(ctx)
    await ctx.rooms_link.stop()
    await h.close()
    hub = ctx.notes_hub
    while hub is not None and hub.tasks:
        await asyncio.gather(*list(hub.tasks), return_exceptions=True)


def frame(frame_id: str, *ops) -> bytes:
    return base64.b64decode(doc_frame(frame_id, *ops))


def meeting(ctx, notes: str = "launch plan\n") -> str:
    s = Session(name="Planning", notes=notes, agenda=[AgendaItem(text="Budget")])
    return ctx.repo.save(s).id


async def until(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def rendered(snapshot_b64url: str) -> str:
    """The markdown of a snapshot as the sidecar folds it."""
    binary = shared_notes._binary()
    snap = base64.b64encode(pf.b64url_decode(snapshot_b64url)).decode()
    reply = shared_notes._run_doc(
        binary, {"op": "fold", "notes": "0" * 64, "snapshot": snap, "frames": []}
    )
    return reply["markdown"]


def snapshot_of(h: FakeRoomHost, row: dict) -> dict:
    _, room = h.room()
    kind, obj = gf.unseal_json(row["key"], row["room"], room.snapshot)
    assert kind is gf.Kind.SNAPSHOT
    return obj


def opened(row: dict, client: GuestClient) -> list[tuple[gf.Kind, object]]:
    """What a guest received in 0x01 envelopes, unsealed."""
    out = []
    for _, env in client.binary(T_NOTES):
        kind, plain = gf.unseal(row["key"], row["room"], env)
        out.append((kind, gf.parse_notes(plain) if kind is gf.Kind.NOTES else json.loads(plain)))
    return out


async def started(ctx, h, sid) -> tuple[dict, guest_notes.Bridge]:
    row = await guest_notes.start(ctx, sid, by="Corey")
    bridge = guest_notes.bridge_for(ctx, sid)
    assert bridge is not None
    await until(lambda: bridge.connected.is_set())
    return row, bridge


async def guest(h, row) -> GuestClient:
    return await GuestClient.join(h, row["room"], row["room_token"])


# ---- start ------------------------------------------------------------------------------


async def test_start_registers_the_room_and_posts_a_snapshot(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    room_id, room = host.room()
    assert row["room"] == room_id and row["host_url"] == host.url
    create, snap = host.calls[0], host.calls[1]
    assert (create[0], create[1]) == ("PUT", f"/live/{room_id}")
    assert create[2]["x-room-token"] == row["room_token"] == gf.room_token(row["key"])
    assert create[2]["authorization"] == f"Bearer {row['token']}"
    assert create[2]["x-room-expires"].endswith("Z")
    assert (snap[0], snap[1], snap[2]["x-room-since"]) == ("PUT", f"/live/{room_id}/snapshot", "0")
    obj = snapshot_of(host, row)
    assert obj["format"] == 1 and obj["threads"] == []
    assert rendered(obj["notes"]) == ctx.repo.get(sid).notes == "launch plan\n"
    assert obj["meta"]["title"] == "Planning" and obj["meta"]["owner"] == "Corey"
    assert obj["meta"]["agenda"] == [{"text": "Budget", "covered": False}]
    assert obj["meta"]["ended"] is False
    # The guest page cannot open the document without its hashseq origin.
    assert obj["origin"] == obj["meta"]["origin"] == shared_notes.notes_origin(sid)
    assert host.joins == [(room_id, 0)]
    link = guest_notes.link(row)
    assert link == f"{host.url}/n/{room_id}#{gf.key_to_fragment(row['key'])}"
    # Started again: the same room.
    assert (await guest_notes.start(ctx, sid))["room"] == room_id
    assert len(host.rooms) == 1
    kinds = [e["kind"] for e in ctx.repo.events(sid)]
    assert "guest_notes_started" in kinds
    # The key is in no history line.
    assert gf.key_to_fragment(row["key"]) not in json.dumps(
        [e["detail"] for e in ctx.repo.events(sid)]
    )


async def test_a_guest_joining_early_gets_the_origin_in_the_meta(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    await until(lambda: any(k is gf.Kind.META for k, _ in opened(row, ana)))
    meta = next(o for k, o in opened(row, ana) if k is gf.Kind.META)
    assert meta["origin"] == shared_notes.notes_origin(sid) and meta["title"] == "Planning"
    await ana.close()


async def test_who_may_start(ctx, host, settings):
    from mnemosyne import access

    theirs = ctx.repo.get(meeting(ctx)).model_copy(update={"shared_from": "ab" * 32})
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="shared this meeting"):
        guest_notes.check_may_start(ctx, theirs)
    settings.team_mode = True
    with pytest.raises(access.Forbidden, match="turned off"):
        await guest_notes.start(ctx, meeting(ctx))
    settings.allow_guest_notes = True
    token = access.current.set(access.Principal("u2", "Bob", access.MEMBER))
    try:
        mine = ctx.repo.save(Session(name="Bob's", owner_id="u2")).id
        with pytest.raises(access.Forbidden, match="owner or an admin"):
            guest_notes.check_may_start(
                ctx, ctx.repo.get(meeting(ctx)).model_copy(update={"owner_id": "u1"})
            )
        guest_notes.check_may_start(ctx, ctx.repo.get(mine))
    finally:
        access.current.reset(token)
    assert host.rooms == {}


async def test_an_end_the_host_refuses_keeps_the_room_live(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    host.status[("POST", "end")] = 503
    with pytest.raises(guest_notes.GuestNotesError):
        await guest_notes.end(ctx, sid, by="Corey")
    assert guest_notes.state(ctx.repo.guest_room(sid)) == "live"
    assert bridge.running and not bridge.closing and not bridge.row["ended_at"]


async def test_no_host_means_unavailable(ctx, settings, sidecar):
    settings.guest_transport = "host"
    settings.publish_host = ""
    settings.guest_host = ""
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="No host for guest notes"):
        await guest_notes.start(ctx, meeting(ctx))


async def test_the_upload_token_goes_to_the_publish_host_only(ctx, host, settings):
    settings.publish_host_token = "open-sesame"
    await guest_notes.start(ctx, meeting(ctx))
    assert all(c[2].get("x-upload-token") == "open-sesame" for c in host.calls)
    assert guest_notes._upload_token(ctx, "https://elsewhere.example") == ""


# ---- frames both ways ---------------------------------------------------------------------


async def test_a_guest_frame_lands_in_the_log_under_the_guest(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    f = frame("g1", ["ins", 0, "from Ana\n"])
    await ana.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "abc", "Ana", f))
    ops = await until(lambda: [o for o in ctx.repo.notes_ops(sid) if o["author_id"] == "guest:abc"])
    assert ops[0]["author"] == "Ana (guest)" and bytes(ops[0]["frame"]) == f
    await until(lambda: bridge.seq >= 1)
    assert ctx.repo.guest_room(sid)["last_seq"] == bridge.seq
    # Acknowledged to Ana, never echoed back to her: neither by the host nor by the bridge.
    await until(lambda: any(t.get("type") == "ack" for t in ana.texts()))
    await asyncio.sleep(0.05)
    assert not [p for k, p in opened(row, ana) if k is gf.Kind.NOTES and p[0] == "abc"]
    _, room = host.room()
    notes = [gf.unseal(row["key"], row["room"], e) for _, e in room.log]
    assert sum(1 for k, p in notes if k is gf.Kind.NOTES and gf.parse_notes(p)[0] == "abc") == 1
    await ana.close()


async def test_an_owner_frame_goes_out_sealed_as_the_owner(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    f = frame("o1", ["ins", 0, "typed at the desk\n"])
    shared_notes.accept_frames(ctx, sid, [f], "", "Corey", can_edit=True)
    got = await until(lambda: [p for k, p in opened(row, ana) if k is gf.Kind.NOTES])
    assert got == [("", "Corey", f)]
    await until(lambda: not bridge.unacked)
    assert bridge.seq >= 1  # acknowledged: in the room's log
    await ana.close()


async def test_frames_from_the_owners_side_in_the_log_are_not_applied_again(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    before = len(ctx.repo.notes_ops(sid))
    env = gf.seal_notes(row["key"], row["room"], "", "Corey", frame("x1", ["ins", 0, "x"]))
    await bridge._on_envelope(env)
    assert len(ctx.repo.notes_ops(sid)) == before


async def test_guest_presence_reaches_the_owners_editors(ctx, host, monkeypatch):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    sent = []
    monkeypatch.setattr(shared_notes, "broadcast_json", lambda app, s, msg: sent.append(msg))
    ana = await guest(host, row)
    presence = gf.seal_json(
        row["key"], row["room"], gf.Kind.PRESENCE, {"gid": "abc", "name": "Ana", "typing": True}
    )
    await ana.send(bytes([T_PRESENCE]) + presence)
    await until(lambda: sent)
    assert sent[-1] == {
        "type": "guests",
        "present": ["Ana (guest)"],
        "typing": ["Ana (guest)"],
        "members": [{"key": "g:abc", "name": "Ana (guest)", "typing": True, "sel": None}],
    }
    assert guest_notes.guests_present(ctx, sid) == ["Ana (guest)"]
    # The owner's heartbeat goes to the room; a guest silent for too long has gone.
    await bridge.tick(now=time.monotonic() + guest_notes.PRESENCE_TTL + 1)
    assert sent[-1] == {"type": "guests", "present": [], "typing": [], "members": []}
    beat = await until(lambda: ana.binary(T_PRESENCE))
    kind, obj = gf.unseal_json(row["key"], row["room"], beat[0][1])
    assert obj == {"gid": "", "name": "Corey", "typing": False}
    await ana.close()


# ---- carets (phase 11) ---------------------------------------------------------------------

S1 = {"a": "1" * 64, "f": "2" * 64}
S2 = {"a": None, "f": "3" * 64}
S3 = {"a": "4" * 64, "f": "4" * 64}


class FakeWs:
    """A desktop editor's WebSocket: records what its member's sender writes."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def send_bytes(self, data) -> None:
        self.sent.append(bytes(data))

    async def close(self, code=1000) -> None:
        self.sent.append(("close", code))


def seal_presence(row: dict, **obj) -> bytes:
    return bytes([T_PRESENCE]) + gf.seal_json(row["key"], row["room"], gf.Kind.PRESENCE, obj)


def presences(row: dict, client: GuestClient, gid: str | None = None) -> list[dict]:
    """The PRESENCE envelopes a guest received, opened (only `gid`'s when given)."""
    out = []
    for _, env in client.binary(T_PRESENCE):
        kind, obj = gf.unseal_json(row["key"], row["room"], env)
        assert kind is gf.Kind.PRESENCE
        if gid is None or obj["gid"] == gid:
            out.append(obj)
    return out


async def test_a_guests_selection_reaches_the_desktop_as_a_caret(ctx, host, monkeypatch):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    sent = []
    monkeypatch.setattr(shared_notes, "broadcast_json", lambda app, s, msg: sent.append(msg))
    ana = await guest(host, row)

    async def say(**obj) -> None:
        n = len(sent)
        await ana.send(seal_presence(row, gid="abc", name="Ana", **obj))
        await until(lambda: len(sent) > n)

    await say(typing=False, sel=S1)
    assert sent == [
        {
            "type": "guests",
            "present": ["Ana (guest)"],
            "typing": [],
            "members": [{"key": "g:abc", "name": "Ana (guest)", "typing": False, "sel": S1}],
        }
    ]
    # Only the selection moved: a caret, not the whole list again.
    await say(typing=False, sel=S2)
    assert sent[1:] == [{"type": "guest_caret", "key": "g:abc", "sel": S2}]
    # A malformed sel is ignored (nothing to say), and one left out keeps the last: the typing
    # change after them carries S2.
    for bad in ({"a": "xyz", "f": None}, {"a": S1["a"]}, "ab" * 32, [1, 2]):
        await ana.send(seal_presence(row, gid="abc", name="Ana", typing=False, sel=bad))
    await say(typing=True)
    assert sent[2:] == [
        {
            "type": "guests",
            "present": ["Ana (guest)"],
            "typing": ["Ana (guest)"],
            "members": [{"key": "g:abc", "name": "Ana (guest)", "typing": True, "sel": S2}],
        }
    ]
    assert bridge.guests["abc"].sel == S2
    # Upper-case hex is the same id; null is no selection.
    await say(typing=True, sel={"a": "A" * 64, "f": "b" * 64})
    await say(typing=True, sel=None)
    assert sent[3:] == [
        {"type": "guest_caret", "key": "g:abc", "sel": {"a": "a" * 64, "f": "b" * 64}},
        {"type": "guest_caret", "key": "g:abc", "sel": None},
    ]
    # The same again is no news.
    n = len(sent)
    await ana.send(seal_presence(row, gid="abc", name="Ana", typing=True, sel=None))
    await asyncio.sleep(0.1)
    assert len(sent) == n
    await ana.close()


async def test_a_guest_saying_gone_is_gone_at_once(ctx, host, monkeypatch):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    sent = []
    monkeypatch.setattr(shared_notes, "broadcast_json", lambda app, s, msg: sent.append(msg))
    ana = await guest(host, row)
    await ana.send(seal_presence(row, gid="abc", name="Ana", typing=True, sel=S1))
    await ana.send(seal_presence(row, gid="def", name="Bob", typing=False))
    await until(lambda: len(guest_notes.guests_present(ctx, sid)) == 2)
    await ana.send(seal_presence(row, gid="abc", name="Ana", gone=True))
    await until(lambda: guest_notes.guests_present(ctx, sid) == ["Bob (guest)"])
    assert sent[-1] == {
        "type": "guests",
        "present": ["Bob (guest)"],
        "typing": [],
        "members": [{"key": "g:def", "name": "Bob (guest)", "typing": False, "sel": None}],
    }
    assert "abc" not in bridge.guests
    await ana.close()


async def test_owner_side_editors_are_sealed_for_the_guests(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    desk = await shared_notes.join(ctx, sid, FakeWs(), "u1", "Corey", True)
    assert desk is not None
    gid = "o:" + desk.key
    # Joining: the room hears the editor at once (the bridge is a presence tap).
    first = await until(lambda: presences(row, ana, gid))
    assert first == [{"gid": gid, "name": "Corey", "typing": False, "sel": None}]
    # Typing, as the hub tells the tap.
    shared_notes.receive_text(ctx, sid, desk, json.dumps({"type": "presence", "typing": True}))
    await until(lambda: len(presences(row, ana, gid)) == 2)
    assert presences(row, ana, gid)[1] == {"gid": gid, "name": "Corey", "typing": True, "sel": None}

    # Caret moves: one at once, then at most one per OWNER_SEL_EVERY, the newest winning.
    loop = asyncio.get_running_loop()
    clock, timers = [loop.time() + 1000.0], []
    real_call_later = loop.call_later

    def call_later(delay, fn, *args):
        if getattr(fn, "__func__", None) is guest_notes.Bridge._flush_owner:
            timers.append((clock[0] + delay, fn, args))
            return type("Handle", (), {"cancel": lambda self: timers.clear()})()
        return real_call_later(delay, fn, *args)

    def advance(seconds: float) -> None:
        clock[0] += seconds
        for due, fn, args in [t for t in timers if t[0] <= clock[0]]:
            timers.remove((due, fn, args))
            fn(*args)

    def caret(sel) -> None:
        bridge._on_room_json({"type": "caret", "key": desk.key, "sel": sel})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(loop, "time", lambda: clock[0])
        mp.setattr(loop, "call_later", call_later)
        caret(S1)  # the window is long over: out at once
        assert len(timers) == 0
        for sel in (S2, S3):
            clock[0] += 0.01
            caret(sel)
        assert len(timers) == 1
        advance(guest_notes.OWNER_SEL_EVERY - 0.02 - 0.001)  # not yet
        advance(0.002)  # the window is over: the newest
        assert timers == []
    await until(lambda: len(presences(row, ana, gid)) == 4)
    await asyncio.sleep(0.05)
    assert [p["sel"] for p in presences(row, ana, gid)] == [None, None, S1, S3]

    # The heartbeat: the "" owner line as in 0.24, and every editor as it is now.
    n = len(presences(row, ana))
    await bridge.tick(now=time.monotonic() + 1000)
    await until(lambda: len(presences(row, ana)) == n + 2)
    assert presences(row, ana)[n:] == [
        {"gid": "", "name": "Corey", "typing": False},
        {"gid": gid, "name": "Corey", "typing": True, "sel": S3},
    ]

    # Leaving: `gone` once.
    await shared_notes.leave(ctx, sid, desk)
    await until(lambda: presences(row, ana, gid)[-1].get("gone"))
    assert presences(row, ana, gid)[-1] == {"gid": gid, "name": "Corey", "gone": True}
    assert bridge.owners == {}
    await asyncio.sleep(0.05)
    assert len(presences(row, ana, gid)) == 6
    await ana.close()


async def test_no_presence_is_queued_while_the_room_is_away(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    desk = await shared_notes.join(ctx, sid, FakeWs(), "u1", "Corey", True)
    assert desk is not None
    await until(lambda: desk.key in bridge.owners)
    room_id, room = host.room()
    room.full = True  # rejoins are refused (4429) until it is not
    for ws in list(room.clients):
        await ws.close()
    await until(lambda: not bridge.connected.is_set())
    bridge._on_room_json({"type": "caret", "key": desk.key, "sel": S1})
    await until(lambda: bridge.owners[desk.key].timer is None)  # its flush came and went
    await bridge.tick(now=time.monotonic() + 1000)
    assert [t for t, _ in list(bridge.outq._queue) if t == T_PRESENCE] == []
    assert bridge.owners[desk.key].sel == S1
    room.full = False
    await until(lambda: bridge.connected.is_set())
    ana = await guest(host, row)
    await bridge.tick(now=time.monotonic() + 2000)
    await until(lambda: len(presences(row, ana)) == 2)
    await asyncio.sleep(0.05)
    # Nothing stale from the time away: the heartbeat says it as it is.
    assert presences(row, ana) == [
        {"gid": "", "name": "Corey", "typing": False},
        {"gid": "o:" + desk.key, "name": "Corey", "typing": False, "sel": S1},
    ]
    await shared_notes.leave(ctx, sid, desk)
    await ana.close()


async def test_presence_left_in_the_queue_is_dropped_on_a_disconnect(ctx, host):
    sid = meeting(ctx)
    row = {
        "room": "0123456789abcdef",
        "key": bytes(range(32)),
        "token": "t",
        "room_token": "rt",
        "host_url": host.url,
    }
    bridge = guest_notes.Bridge(ctx, sid, row)  # never started: nothing reads the queue
    for item in ((T_NOTES, b"n1"), (T_PRESENCE, b"p1"), (T_NOTES, b"n2"), (T_PRESENCE, b"p2")):
        bridge.outq.put_nowait(item)
    bridge._drop_queued_presence()
    assert list(bridge.outq._queue) == [(T_NOTES, b"n1"), (T_NOTES, b"n2")]


# ---- comments ---------------------------------------------------------------------------------


def _comment(row, **fields) -> bytes:
    obj = {
        "op": "create",
        "id": "0123456789ab",
        "parent_id": "",
        "text": "Why?",
        "quote": "launch",
        "gid": "abc",
        "name": "Ana",
        "notes_ids": ["0", "5"],
        **fields,
    }
    return bytes([T_NOTES]) + gf.seal_json(row["key"], row["room"], gf.Kind.COMMENT, obj)


async def test_a_guest_comment_becomes_a_marked_comment(ctx, host, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    await ana.send(_comment(row))
    c = await until(lambda: ctx.repo.get_comment("0123456789ab"))
    assert (c.author_id, c.author, c.target, c.quote) == (
        "guest:abc",
        "Ana (guest)",
        "notes",
        "launch",
    )
    marks = lambda: [r for r in sidecar.doc_requests if r["op"] == "mark_comment"]  # noqa: E731
    await until(marks)
    assert marks()[0]["comment"] == "0123456789ab"
    # The mark reaches the room as a notes frame, and the threads follow.
    threads = await until(
        lambda: [p for k, p in opened(row, ana) if k is gf.Kind.COMMENT and p["threads"]]
    )
    root = threads[-1]["threads"][0]["root"]
    assert root["id"] == "0123456789ab" and root["gid"] == "abc"
    assert root["author"] == "Ana (guest)" and root["mark"] == "comment:0123456789ab"
    assert "author_id" not in root and "mentions" not in root
    await until(lambda: [p for k, p in opened(row, ana) if k is gf.Kind.NOTES])
    # Sent twice (a page resending): one comment, one mark.
    await ana.send(_comment(row))
    await until(lambda: bridge.seq >= 3)
    await asyncio.sleep(0.05)
    assert len(marks()) == 1
    # A reply and an edit; another guest cannot edit Ana's.
    await ana.send(
        _comment(
            row, op="reply", id="0123456789ac", parent_id="0123456789ab", gid="def", name="Ben"
        )
    )
    reply = await until(lambda: ctx.repo.get_comment("0123456789ac"))
    assert (reply.author, reply.parent_id) == ("Ben (guest)", "0123456789ab")
    await ana.send(_comment(row, op="edit", text="Hijacked", gid="def"))
    await ana.send(_comment(row, op="edit", text="Why not?"))
    await until(lambda: ctx.repo.get_comment("0123456789ab").text == "Why not?")
    await ana.close()


# ---- end, stop, resume ------------------------------------------------------------------------


async def test_end_posts_a_final_snapshot_and_turns_the_room_read_only(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    host.calls.clear()
    ended = await guest_notes.end(ctx, sid, by="Corey")
    assert ended is not None and ended["ended_at"] and ended["expires_at"]
    assert guest_notes.state(ended) == "ended"
    assert [(c[0], c[1].rsplit("/", 1)[-1]) for c in host.calls] == [
        ("PUT", "snapshot"),
        ("POST", "end"),
    ]
    assert host.calls[1][2]["x-room-expires"].endswith("Z")
    assert snapshot_of(host, row)["meta"]["ended"] is True
    assert host.room()[1].ended
    assert guest_notes.bridge_for(ctx, sid) is None and not bridge.running
    assert bridge.member not in (
        ctx.notes_hub.rooms.get(sid).members if sid in ctx.notes_hub.rooms else []
    )
    await until(lambda: any(t.get("type") == "ended" for t in ana.texts()))
    assert await guest_notes.end(ctx, sid) is None  # again: nothing
    # One line, though the host also told the bridge it ended.
    assert [e["kind"] for e in ctx.repo.events(sid)].count("guest_notes_ended") == 1
    await ana.close()


async def test_stop_deletes_the_room(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    host.calls.clear()
    stopped = await guest_notes.stop(ctx, sid, by="Corey")
    assert stopped is not None and guest_notes.state(stopped) == "stopped"
    assert [(c[0], c[1]) for c in host.calls] == [("DELETE", f"/live/{row['room']}")]
    assert host.rooms[row["room"]].deleted
    assert guest_notes.bridge_for(ctx, sid) is None
    await until(lambda: ana.closed_code == 4410)
    assert [e["kind"] for e in ctx.repo.events(sid)].count("guest_notes_stopped") == 1
    # A new link is a new room.
    fresh = await guest_notes.new_link(ctx, sid, by="Corey")
    assert fresh["room"] != row["room"] and fresh["key"] != row["key"]
    assert guest_notes.state(ctx.repo.guest_room(sid)) == "live"


async def test_resume_rejoins_from_the_last_seq_and_catches_up(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    f1 = frame("g1", ["ins", 0, "one\n"])
    await ana.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "abc", "Ana", f1))
    await until(lambda: ctx.repo.guest_room(sid)["last_seq"] >= 1)
    await guest_notes.shutdown(ctx)
    assert guest_notes.bridge_for(ctx, sid) is None
    seen = ctx.repo.guest_room(sid)["last_seq"]
    # Ana keeps typing while the owner's app is away.
    f2 = frame("g2", ["ins", 0, "two\n"])
    await ana.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "abc", "Ana", f2))
    await until(lambda: host.room()[1].seq > seen)
    assert await guest_notes.resume(ctx) == 1
    bridge = guest_notes.bridge_for(ctx, sid)
    await until(lambda: bridge is not None and bridge.connected.is_set())
    assert host.joins[-1] == (row["room"], seen)
    ops = await until(
        lambda: (
            [bytes(o["frame"]) for o in ctx.repo.notes_ops(sid) if o["author_id"] == "guest:abc"]
            if len([o for o in ctx.repo.notes_ops(sid) if o["author_id"] == "guest:abc"]) == 2
            else None
        )
    )
    assert ops == [f1, f2]
    await ana.close()


async def test_a_room_the_host_deleted_stops_the_bridge(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    room = host.rooms[row["room"]]
    room.deleted = True
    for ws in list(room.clients):
        await ws.close(4410)
    await until(lambda: not bridge.running)
    assert guest_notes.state(ctx.repo.guest_room(sid)) == "stopped"


async def test_the_recording_stopping_schedules_the_end(ctx, host, settings):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    guest_notes.recording_stopped(ctx, sid)
    due = guest_notes._parse(ctx.repo.guest_room(sid)["auto_end_at"])
    hours = (due - guest_notes._now()).total_seconds() / 3600
    assert hours == pytest.approx(settings.guest_notes_end_hours, abs=0.01)
    ctx.repo.set_guest_room(sid, auto_end_at="2000-01-01T00:00:00+00:00")
    await bridge.tick(now=time.monotonic() + guest_notes.AUTO_END_EVERY + 1)
    await until(lambda: guest_notes.state(ctx.repo.guest_room(sid)) == "ended")
    await until(lambda: not bridge.running)


async def test_a_full_room_is_retried_not_given_up(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    room = host.rooms[row["room"]]
    room.full = True
    for ws in list(room.clients):
        await ws.close(1012)  # the connection drops; every retry is then told 4429
    await until(lambda: len(host.joins) >= 1 and not bridge.connected.is_set())
    await asyncio.sleep(0.15)  # several backoffs (0.01 s)
    assert bridge.running
    assert guest_notes.state(ctx.repo.guest_room(sid)) != "stopped"
    room.full = False
    await until(lambda: bridge.connected.is_set())


async def test_a_room_deleted_while_away_is_closed_gone_at_the_upgrade(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    await guest_notes.shutdown(ctx)
    host.rooms[row["room"]].deleted = True
    assert await guest_notes.resume(ctx) == 1
    await until(lambda: guest_notes.state(ctx.repo.guest_room(sid)) == "stopped")
    await until(lambda: guest_notes.bridge_for(ctx, sid) is None)


async def test_a_lagged_bridge_rejoins_with_its_seq(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    f = frame("g1", ["ins", 0, "one\n"])
    await ana.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "abc", "Ana", f))
    room = host.rooms[row["room"]]
    await until(lambda: bridge.seq == room.seq >= 2)  # its meta, Ana's frame
    seen = bridge.seq
    # The bridge's socket: not Ana's.
    bridge_ws = next(ws for ws in room.clients if ws.remote_address != ana.ws.local_address)
    await bridge_ws.close(4409, "lagged")
    await until(lambda: len(host.joins) == 3)  # the bridge, Ana, the bridge again
    assert host.joins[-1] == (row["room"], seen)
    await ana.close()


# ---- comments, one behaviour at a time ---------------------------------------------------------


def marks(sidecar) -> list[dict]:
    return [r for r in sidecar.doc_requests if r["op"] == "mark_comment"]


async def test_a_guest_comment_is_the_guests_and_marked_once(ctx, host, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    await ana.send(_comment(row))
    c = await until(lambda: ctx.repo.get_comment("0123456789ab"))
    assert (c.author_id, c.author, c.target, c.parent_id) == (
        "guest:abc",
        "Ana (guest)",
        "notes",
        "",
    )
    await until(lambda: marks(sidecar))
    assert [(m["comment"], m["first"], m["last"]) for m in marks(sidecar)] == [
        ("0123456789ab", "0", "5")
    ]
    # The comment is on the meeting, as the owner reads it (a mark is on its text).
    assert ctx.repo.get_comment("0123456789ab").text == "Why?"
    await ana.close()


async def test_a_resent_create_is_not_marked_again(ctx, host, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    await ana.send(_comment(row))
    await until(lambda: marks(sidecar))
    await ana.send(_comment(row))
    await ana.send(_comment(row))
    # A sentinel behind them: once it is in, the resends have been handled.
    await ana.send(_comment(row, id="0123456789ad", notes_ids=[]))
    await until(lambda: ctx.repo.get_comment("0123456789ad"))
    assert len(marks(sidecar)) == 1
    mine = [t for t in ctx.repo.list_comments(sid) if t.id == "0123456789ab"]
    assert len(mine) == 1 and mine[0].text == "Why?"
    await ana.close()


async def test_a_reply_attaches_to_its_parent(ctx, host, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    await ana.send(_comment(row))
    await until(lambda: ctx.repo.get_comment("0123456789ab"))
    await ana.send(
        _comment(
            row,
            op="reply",
            id="0123456789ac",
            parent_id="0123456789ab",
            gid="def",
            name="Ben",
            text="Because.",
            notes_ids=[],
        )
    )
    reply = await until(lambda: ctx.repo.get_comment("0123456789ac"))
    assert (reply.author, reply.author_id, reply.parent_id, reply.text) == (
        "Ben (guest)",
        "guest:def",
        "0123456789ab",
        "Because.",
    )
    assert len(marks(sidecar)) == 1  # a reply has no range of its own
    threads = guest_notes.threads_for_guests(ctx, sid)
    assert [r["id"] for r in threads[0]["replies"]] == ["0123456789ac"]
    await ana.close()


async def test_a_guest_changes_only_their_own_comments(ctx, host, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    await ana.send(_comment(row))
    await until(lambda: ctx.repo.get_comment("0123456789ab"))
    # Another guest (gid "def") tries to edit and delete it: refused, nothing changes.
    await ana.send(_comment(row, op="edit", text="Hijacked", gid="def", name="Ben"))
    await ana.send(_comment(row, op="delete", gid="def", name="Ben"))
    # Ana's own edit behind them: when it shows, the refusals have been handled.
    await ana.send(_comment(row, op="edit", text="Why not?"))
    await until(lambda: ctx.repo.get_comment("0123456789ab").text == "Why not?")
    mine = ctx.repo.get_comment("0123456789ab")
    assert not mine.deleted and mine.author_id == "guest:abc"
    # Her own delete is taken.
    await ana.send(_comment(row, op="delete"))
    await until(lambda: ctx.repo.get_comment("0123456789ab") is None)  # no replies: gone
    await ana.close()


async def test_threads_are_sealed_to_the_room_after_a_comments_event(ctx, host, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    ben = await guest(host, row)
    await ana.send(_comment(row, text="Why? @corey"))
    await until(lambda: ctx.repo.get_comment("0123456789ab"))
    # Ben, another page, gets the threads as a sealed COMMENT envelope in the log.
    got = await until(
        lambda: [
            p
            for k, p in opened(row, ben)
            if k is gf.Kind.COMMENT and p.get("op") == "threads" and p["threads"]
        ]
    )
    assert set(got[-1]) == {"op", "threads"}
    (thread,) = got[-1]["threads"]
    assert thread["root"]["id"] == "0123456789ab" and thread["root"]["text"] == "Why? @corey"
    assert thread["replies"] == []
    assert set(thread["root"]) == {
        "id",
        "parent_id",
        "author",
        "gid",
        "text",
        "quote",
        "mark",
        "created_at",
        "updated_at",
        "resolved",
        "deleted",
        "detached",
        "changed",
    }
    # An event with nothing new still re-seals them (the owner resolved a thread, say).
    before = len([1 for k, p in opened(row, ben) if k is gf.Kind.COMMENT])
    ctx.bus.publish({"type": "comments", "session_id": sid, "mentions": []})
    await until(lambda: len([1 for k, p in opened(row, ben) if k is gf.Kind.COMMENT]) > before)
    # Another meeting's event is no news here (once the mark's own events have settled).
    await asyncio.sleep(0.3)
    seen = len([1 for k, p in opened(row, ben) if k is gf.Kind.COMMENT])
    ctx.bus.publish({"type": "comments", "session_id": "someone-else", "mentions": []})
    await asyncio.sleep(0.1)
    assert len([1 for k, p in opened(row, ben) if k is gf.Kind.COMMENT]) == seen
    # And what reaches the host never carries user ids or mentions, in the snapshot either.
    _, room = host.room()
    raw = [gf.unseal(row["key"], row["room"], e)[1] for _, e in room.log]
    assert not [b for b in raw if b"author_id" in b or b"mentions" in b]
    await bridge.snapshot()
    assert "author_id" not in json.dumps(snapshot_of(host, row)["threads"])
    await ana.close()
    await ben.close()


# ---- the meeting's details ----------------------------------------------------------------------


def metas(row, client) -> list[dict]:
    return [o for k, o in opened(row, client) if k is gf.Kind.META]


async def test_meta_is_sent_again_when_the_agenda_changes(app, ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    first = await until(lambda: metas(row, ana))
    assert first[-1]["agenda"] == [{"text": "Budget", "covered": False}]
    sent = len(first)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.put(
            f"/api/sessions/{sid}/agenda",
            json={"items": [{"text": "Budget", "covered": True}, {"text": "Hiring"}]},
        )
        assert r.status_code == 200, r.text
    await until(lambda: len(metas(row, ana)) > sent)
    meta = metas(row, ana)[-1]
    assert meta["agenda"] == [
        {"text": "Budget", "covered": True},
        {"text": "Hiring", "covered": False},
    ]
    assert meta["origin"] == shared_notes.notes_origin(sid) and meta["owner"] == "Corey"
    # The same details again are not sent twice.
    sent = len(metas(row, ana))
    ctx.bus.publish({"type": "session", "session_id": sid, "status": "created"})
    await asyncio.sleep(0.1)
    assert len(metas(row, ana)) == sent
    await ana.close()


async def test_meta_follows_the_copilot_marking_agenda_points_covered(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    sent = len(await until(lambda: metas(row, ana)))
    # As services/copilot.py does: the agenda saved with the point marked, then the event.
    current = ctx.repo.get(sid)
    ctx.repo.update_fields(
        sid, agenda=[a.model_copy(update={"covered": True}) for a in current.agenda]
    )
    ctx.bus.publish({"type": "copilot_notes", "session_id": sid, "notes": {"lines": 3}})
    await until(lambda: len(metas(row, ana)) > sent)
    assert metas(row, ana)[-1]["agenda"] == [{"text": "Budget", "covered": True}]
    assert metas(row, ana)[-1]["origin"] == shared_notes.notes_origin(sid)
    await ana.close()


# ---- snapshots over time ------------------------------------------------------------------------


def snapshots(host) -> int:
    return len([c for c in host.calls if c[0] == "PUT" and c[1].endswith("/snapshot")])


async def test_the_snapshot_timer_posts_only_when_something_changed(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    base = snapshots(host)
    later = time.monotonic() + guest_notes.SNAPSHOT_EVERY + 1
    # Nothing changed since the last one: due, but not posted.
    bridge.dirty = False
    await bridge.tick(now=later)
    assert snapshots(host) == base
    # Changed, but not due yet: not posted either.
    bridge.dirty = True
    await bridge.tick(now=bridge._last_snapshot + guest_notes.SNAPSHOT_EVERY - 1)
    assert snapshots(host) == base and bridge.dirty
    # Changed and due: posted once, then clean again.
    await bridge.tick(now=bridge._last_snapshot + guest_notes.SNAPSHOT_EVERY + 1)
    assert snapshots(host) == base + 1 and not bridge.dirty
    await bridge.tick(now=later + 2 * guest_notes.SNAPSHOT_EVERY)
    assert snapshots(host) == base + 1


async def test_a_typed_frame_makes_the_next_snapshot_due(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    bridge.dirty = False
    base = snapshots(host)
    shared_notes.accept_frames(
        ctx, sid, [frame("o9", ["ins", 0, "typed\n"])], "", "Corey", can_edit=True
    )
    await until(lambda: bridge.dirty)
    await bridge.tick(now=bridge._last_snapshot + guest_notes.SNAPSHOT_EVERY + 1)
    assert snapshots(host) == base + 1
    assert "typed" in rendered(snapshot_of(host, row)["notes"])


async def test_end_posts_the_last_typing_before_it_closes_the_room(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    shared_notes.accept_frames(
        ctx, sid, [frame("o8", ["ins", 0, "last words\n"])], "", "Corey", can_edit=True
    )
    host.calls.clear()
    await guest_notes.end(ctx, sid, by="Corey")
    order = [c[1].rsplit("/", 1)[-1] for c in host.calls]
    assert order.index("snapshot") < order.index("end")
    assert "last words" in rendered(snapshot_of(host, row)["notes"])
    ended = await until(lambda: [t for t in ana.texts() if t.get("type") == "ended"])
    assert ended[0]["expires"]
    await ana.close()


async def test_resume_does_not_apply_our_own_frames_again(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    shared_notes.accept_frames(
        ctx, sid, [frame("o7", ["ins", 0, "mine\n"])], "", "Corey", can_edit=True
    )
    await until(lambda: bridge.seq >= 1 and not bridge.unacked)
    seen = ctx.repo.guest_room(sid)["last_seq"]
    assert seen == bridge.seq >= 1
    ops_before = len(ctx.repo.notes_ops(sid))
    await guest_notes.shutdown(ctx)
    # Resumed at the seq it had: it asks for nothing older.
    assert await guest_notes.resume(ctx) == 1
    again = guest_notes.bridge_for(ctx, sid)
    await until(lambda: again is not None and again.connected.is_set())
    assert host.joins[-1] == (row["room"], seen)
    await guest_notes.shutdown(ctx)
    # A crash before the seq was saved: the whole log comes back, owner frames included.
    ctx.repo.set_guest_room(sid, last_seq=0)
    assert await guest_notes.resume(ctx) == 1
    again = guest_notes.bridge_for(ctx, sid)
    await until(lambda: again is not None and again.connected.is_set())
    assert host.joins[-1] == (row["room"], 0)
    await until(lambda: again.seq >= seen)
    assert len(ctx.repo.notes_ops(sid)) == ops_before
    assert ctx.repo.get(sid).notes.count("mine") <= 1


# ---- the automatic end ------------------------------------------------------------------------


class _Recording:
    is_recording = True


async def test_auto_end_fires_after_the_configured_hours(ctx, host, settings, monkeypatch):
    settings.guest_notes_end_hours = 2
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    guest_notes.recording_stopped(ctx, sid)
    real = guest_notes._now
    # An hour on: not yet.
    monkeypatch.setattr(guest_notes, "_now", lambda: real() + guest_notes.timedelta(hours=1))
    await bridge.tick(now=time.monotonic() + guest_notes.AUTO_END_EVERY + 1)
    await asyncio.sleep(0.1)
    assert guest_notes.state(ctx.repo.guest_room(sid)) == "live" and bridge.running
    # Past the second hour: the room ends, by nobody, and says so in the history.
    monkeypatch.setattr(
        guest_notes, "_now", lambda: real() + guest_notes.timedelta(hours=2, minutes=1)
    )
    await bridge.tick(now=time.monotonic() + 3 * guest_notes.AUTO_END_EVERY)
    await until(lambda: guest_notes.state(ctx.repo.guest_room(sid)) == "ended")
    assert [e for e in ctx.repo.events(sid) if e["kind"] == "guest_notes_ended"]


async def test_auto_end_does_not_fire_while_the_meeting_is_recording(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ctx.repo.set_guest_room(sid, auto_end_at="2000-01-01T00:00:00+00:00")
    ctx.active_recordings[sid] = _Recording()
    try:
        await bridge.tick(now=time.monotonic() + guest_notes.AUTO_END_EVERY + 1)
        await asyncio.sleep(0.1)
        assert guest_notes.state(ctx.repo.guest_room(sid)) == "live" and bridge.running
    finally:
        del ctx.active_recordings[sid]
    # Not recording any more: the next check ends it.
    await bridge.tick(now=time.monotonic() + 3 * guest_notes.AUTO_END_EVERY)
    await until(lambda: guest_notes.state(ctx.repo.guest_room(sid)) == "ended")


async def test_a_new_recording_part_cancels_the_scheduled_end(ctx, host):
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    guest_notes.recording_stopped(ctx, sid)
    assert ctx.repo.guest_room(sid)["auto_end_at"]
    guest_notes.recording_started(ctx, sid)
    assert ctx.repo.guest_room(sid)["auto_end_at"] == ""
    # A tick, long after, ends nothing.
    await bridge.tick(now=time.monotonic() + 10 * guest_notes.AUTO_END_EVERY)
    await asyncio.sleep(0.1)
    assert guest_notes.state(ctx.repo.guest_room(sid)) == "live"
    # Nothing to cancel for an ended room or a meeting with none: no error.
    guest_notes.recording_started(ctx, "no-such-meeting")
    await guest_notes.end(ctx, sid)
    guest_notes.recording_started(ctx, sid)


async def test_starting_a_recording_through_the_api_cancels_the_scheduled_end(
    app, ctx, host, fake_pipewire, settings
):
    settings.live_transcription = False
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    guest_notes.recording_stopped(ctx, sid)  # the first part stopped a moment ago
    assert ctx.repo.guest_room(sid)["auto_end_at"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/api/audio/start", json={"device_ids": [1], "session_id": sid})
        assert r.status_code == 200, r.text
    try:
        assert ctx.repo.guest_room(sid)["auto_end_at"] == ""
    finally:
        ctx.active_recordings.pop(sid, None)


async def test_meta_carries_the_part_clock_while_the_meeting_records(
    app, ctx, host, fake_pipewire, settings
):
    """Guests' time chips: META gets `clock` when a recording starts and loses it at stop."""
    settings.live_transcription = False
    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    first = await until(lambda: metas(row, ana))
    assert "clock" not in first[-1]
    sent = len(first)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/api/audio/start", json={"device_ids": [1], "session_id": sid})
        assert r.status_code == 200, r.text
        clock = r.json()["clock"]
        await until(lambda: len(metas(row, ana)) > sent)
        meta = metas(row, ana)[-1]
        assert meta["clock"] == clock and clock["part"] == 0 and clock["offset"] == 0.0
        assert guest_notes.meta_for_guests(ctx, sid)["clock"] == clock
        sent = len(metas(row, ana))
        r = await c.post(f"/api/audio/stop/{sid}", json={"transcribe": False})
        assert r.status_code == 200, r.text
        await ctx.jobs.wait(r.json()["job_id"])
    await until(lambda: len(metas(row, ana)) > sent)
    assert "clock" not in metas(row, ana)[-1]
    assert "clock" not in guest_notes.meta_for_guests(ctx, sid)
    await ana.close()


async def test_a_new_part_sends_its_clock_to_the_guests(ctx, host):
    """A capture restart stops one part and starts the next: META follows both."""
    from mnemosyne.audio.capture import RecordingSession

    sid = meeting(ctx)
    row, bridge = await started(ctx, host, sid)
    ana = await guest(host, row)
    sent = len(await until(lambda: metas(row, ana)))
    part = RecordingSession(session_id="rec2", output_dir=ctx.settings.recordings_dir, part=1)
    part.is_recording = True
    part.clock_start = (1, 300.0)
    ctx.active_recordings[sid] = part
    try:
        guest_notes.recording_started(ctx, sid)
        await until(lambda: len(metas(row, ana)) > sent)
        clock = metas(row, ana)[-1]["clock"]
        assert clock["part"] == 1 and clock["offset"] == 300.0 and clock["started_at"]
        # The same clock again is not sent twice.
        sent = len(metas(row, ana))
        guest_notes.recording_started(ctx, sid)
        await asyncio.sleep(0.1)
        assert len(metas(row, ana)) == sent
    finally:
        del ctx.active_recordings[sid]
    guest_notes.recording_stopped(ctx, sid)
    await until(lambda: len(metas(row, ana)) > sent)
    assert "clock" not in metas(row, ana)[-1]
    await ana.close()


# ---- iroh rooms on this computer (phase 10) ------------------------------------------------

ENDPOINT = "a" * 64
N0_RELAY = "https://use1-1.relay.n0.iroh.link./"


@pytest.fixture
async def iroh(ctx, settings, sidecar, monkeypatch):
    """`guest_transport` auto with the rooms sidecar's loopback host played by a FakeRoomHost
    (the fake sidecar's `listen` is its address) and no publish host."""
    from mnemosyne.services import link as link_service

    monkeypatch.setattr(link_service, "RESTART_DELAY", 0.01)
    loop = await FakeRoomHost().start()
    sidecar.config.update(listen=loop.url.removeprefix("http://"), endpoint_id=ENDPOINT)
    sidecar.config["relay"] = N0_RELAY
    sidecar.sync()
    ctx.http_transport = routed(loop)
    settings.guest_transport = "auto"
    settings.remote_relays = N0_RELAY  # a relay is configured (blank + ours off = no iroh)
    settings.publish_host = ""
    settings.local_speaker_name = "Corey"
    yield loop
    await guest_notes.shutdown(ctx)
    await ctx.rooms_link.stop()
    await loop.close()
    hub = ctx.notes_hub
    while hub is not None and hub.tasks:
        await asyncio.gather(*list(hub.tasks), return_exceptions=True)


@pytest.fixture
async def mirror(ctx, settings, iroh):
    """A publish host beside the iroh room: the room is mirrored there."""
    h = await FakeRoomHost().start()
    ctx.http_transport = routed(iroh, h)
    settings.publish_host = h.url
    yield h
    await guest_notes.shutdown(ctx)
    await h.close()


def room_ops(sidecar, op: str) -> list[dict]:
    return [o for o in sidecar.ops if o["op"] == op]


def notes_in(row: dict, room) -> list[tuple[str, str, bytes]]:
    """(gid, name, frame) of the notes envelopes in a fake room's log."""
    out = []
    for _, env in room.log:
        kind, plain = gf.unseal(row["key"], row["room"], env)
        if kind is gf.Kind.NOTES:
            out.append(gf.parse_notes(plain))
    return out


async def test_auto_opens_an_iroh_room_on_this_computer(ctx, iroh, sidecar):
    sid = meeting(ctx)
    row, bridge = await started(ctx, iroh, sid)
    assert row["transport"] == "iroh" and row["endpoint_id"] == ENDPOINT
    assert row["relay"] == N0_RELAY and row["mirror_host_url"] == ""
    assert row["host_url"] == iroh.url == ctx.rooms_link.base_url
    room_id, room = iroh.room()
    assert room_id == row["room"] and room.room_token == row["room_token"]
    # Made on the loopback host as on a publish host, with the sidecar's upload token.
    assert [(c[0], c[1]) for c in iroh.calls[:2]] == [
        ("PUT", f"/live/{room_id}"),
        ("PUT", f"/live/{room_id}/snapshot"),
    ]
    token = ctx.rooms_link.upload_token
    assert token and all(c[2].get("x-upload-token") == token for c in iroh.calls)
    # Served without a relay asked for: the sidecar picks one, the row keeps it.
    assert room_ops(sidecar, "room_start") == [{"op": "room_start", "room": room_id}]
    assert iroh.joins == [(room_id, 0)]
    key = gf.key_to_fragment(row["key"])
    link = guest_notes.link(row)
    assert link == (
        f"{guest_notes.GUEST_PAGE_ORIGIN}#2.{room_id}.{key}.{ENDPOINT}.use1-1.relay.n0.iroh.link."
    )
    assert guest_notes.link(row, "https://pages.example/n/").startswith(
        "https://pages.example/n/#2."
    )
    assert guest_notes.relay_kind(row["relay"]) == "n0"
    # A guest arriving through the sidecar's proxy is in the loopback room like any other.
    ana = await guest(iroh, row)
    f = frame("g1", ["ins", 0, "from Ana\n"])
    await ana.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "abc", "Ana", f))
    await until(lambda: [o for o in ctx.repo.notes_ops(sid) if o["author_id"] == "guest:abc"])
    await until(lambda: ctx.repo.guest_room(sid)["last_seq"] >= 1)
    assert ctx.repo.guest_room(sid)["mirror_last_seq"] == 0
    await ana.close()


def test_format_2_keeps_the_relay_host_and_port():
    row = {
        "room": "0123456789abcdef",
        "key": bytes(range(32)),
        "transport": "iroh",
        "host_url": "http://127.0.0.1:5",
        "endpoint_id": ENDPOINT,
        "relay": "http://127.0.0.1:3340/",
        "mirror_host_url": "",
    }
    key = gf.key_to_fragment(row["key"])
    assert guest_notes.link(row, "https://x.example/n/") == (
        f"https://x.example/n/#2.{row['room']}.{key}.{ENDPOINT}.127.0.0.1:3340"
    )
    assert guest_notes.relay_host("https://[::1]:8443") == "[::1]:8443"
    assert guest_notes.served_from(row, "https://x.example/n/") == "https://x.example/n/"
    row["mirror_host_url"] = "https://host.example"
    assert guest_notes.link(row).startswith(f"https://host.example/n/{row['room']}#2.")
    assert guest_notes.served_from(row) == "https://host.example/n/"
    host_row = {**row, "transport": "host", "host_url": "https://host.example"}
    assert guest_notes.link(host_row) == f"https://host.example/n/{row['room']}#{key}"


async def test_a_mirrored_room_is_on_both_and_frames_cross_once(ctx, iroh, mirror):
    sid = meeting(ctx)
    row, bridge = await started(ctx, iroh, sid)
    other = guest_notes.bridge_for(ctx, sid, guest_notes.MIRROR)
    assert other is not None
    await until(lambda: other.connected.is_set())
    assert row["transport"] == "iroh" and row["mirror_host_url"] == mirror.url
    # The same room (id, room token) on the mirror, with its own publisher token.
    (m_id, m_room), (l_id, l_room) = mirror.room(), iroh.room()
    assert m_id == l_id == row["room"] and m_room.room_token == l_room.room_token
    assert m_room.token == row["mirror_token"] != row["token"] == l_room.token
    assert not any("x-upload-token" in c[2] for c in mirror.calls)  # no publish_host_token
    assert m_room.snapshot is not None and l_room.snapshot is not None
    # The link names the mirror's page, so the guest page can fall back to its WebSocket.
    key = gf.key_to_fragment(row["key"])
    assert guest_notes.link(row) == (
        f"{mirror.url}/n/{row['room']}#2.{row['room']}.{key}.{ENDPOINT}.use1-1.relay.n0.iroh.link."
    )
    # Ana is on iroh, Bob on the mirror.
    ana = await guest(iroh, row)
    bob = await guest(mirror, row)
    fa = frame("ga", ["ins", 0, "from Ana\n"])
    await ana.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "abc", "Ana", fa))
    got = await until(lambda: [p for k, p in opened(row, bob) if k is gf.Kind.NOTES])
    assert got == [("", "Corey", fa)]  # sealed as the owner's side
    fb = frame("gb", ["ins", 0, "from Bob\n"])
    await bob.send(bytes([T_NOTES]) + gf.seal_notes(row["key"], row["room"], "def", "Bob", fb))
    got = await until(lambda: [p for k, p in opened(row, ana) if k is gf.Kind.NOTES])
    assert got == [("", "Corey", fb)]
    await until(lambda: bridge.seq >= 2 and other.seq >= 2 and not bridge.unacked)
    await until(lambda: not other.unacked)
    await asyncio.sleep(0.1)
    # No loop: each frame once in each room, accepted once, never back to its writer.
    assert notes_in(row, l_room) == [("abc", "Ana", fa), ("", "Corey", fb)]
    assert notes_in(row, m_room) == [("", "Corey", fa), ("def", "Bob", fb)]
    authors = [o["author_id"] for o in ctx.repo.notes_ops(sid)]
    assert authors.count("guest:abc") == 1 and authors.count("guest:def") == 1
    assert [p for k, p in opened(row, ana) if k is gf.Kind.NOTES] == [("", "Corey", fb)]
    stored = ctx.repo.guest_room(sid)
    assert stored["last_seq"] == bridge.seq and stored["mirror_last_seq"] == other.seq
    # Guests in either room are the meeting's guests.
    for client, gid, name in ((ana, "abc", "Ana"), (bob, "def", "Bob")):
        presence = gf.seal_json(
            row["key"], row["room"], gf.Kind.PRESENCE, {"gid": gid, "name": name, "typing": False}
        )
        await client.send(bytes([T_PRESENCE]) + presence)
    await until(lambda: len(guest_notes.guests_present(ctx, sid)) == 2)
    assert sorted(guest_notes.guests_present(ctx, sid)) == ["Ana (guest)", "Bob (guest)"]
    await ana.close()
    await bob.close()


async def test_guest_presence_crosses_the_mirror_byte_for_byte(ctx, iroh, mirror):
    sid = meeting(ctx)
    row, bridge = await started(ctx, iroh, sid)
    other = guest_notes.bridge_for(ctx, sid, guest_notes.MIRROR)
    assert other is not None
    await until(lambda: other.connected.is_set())
    ana = await guest(iroh, row)  # on iroh
    bob = await guest(mirror, row)  # on the mirror
    pa = seal_presence(row, gid="abc", name="Ana", typing=True, sel=S1)
    pb = seal_presence(row, gid="def", name="Bob", typing=False, sel=None)
    await ana.send(pa)
    await until(lambda: bob.binary(T_PRESENCE))
    await bob.send(pb)
    await until(lambda: ana.binary(T_PRESENCE))
    await asyncio.sleep(0.1)
    # Exactly the envelope each sent, once, and never back to its sender.
    assert bob.binary(T_PRESENCE) == [(0, pa[1:])]
    assert ana.binary(T_PRESENCE) == [(0, pb[1:])]
    # The desktop sees both, each from the bridge that heard it first-hand.
    assert sorted(m["key"] for m in guest_notes._guest_members(ctx, sid)) == ["g:abc", "g:def"]
    assert set(bridge.guests) == {"abc"} and set(other.guests) == {"def"}
    # Owner-side presence is sealed by each bridge for its own room, never forwarded.
    desk = await shared_notes.join(ctx, sid, FakeWs(), "u1", "Corey", True)
    assert desk is not None
    gid = "o:" + desk.key
    await until(lambda: presences(row, ana, gid) and presences(row, bob, gid))
    await bridge.tick(now=time.monotonic() + 1000)  # the primary's "" heartbeat: iroh only
    await until(lambda: presences(row, ana, ""))
    await asyncio.sleep(0.1)
    assert presences(row, bob, "") == []
    assert len(presences(row, ana, gid)) == 2 and len(presences(row, bob, gid)) == 1
    # A guest leaving is forwarded too.
    gone = seal_presence(row, gid="abc", name="Ana", gone=True)
    await ana.send(gone)
    await until(lambda: len(bob.binary(T_PRESENCE)) > 2)
    assert bob.binary(T_PRESENCE)[-1] == (0, gone[1:])
    await until(lambda: [m["key"] for m in guest_notes._guest_members(ctx, sid)] == ["g:def"])
    await shared_notes.leave(ctx, sid, desk)
    await ana.close()
    await bob.close()


async def test_stop_removes_both_rooms_and_stops_serving(ctx, iroh, mirror, sidecar):
    sid = meeting(ctx)
    row, _ = await started(ctx, iroh, sid)
    stopped = await guest_notes.stop(ctx, sid, by="Corey")
    assert guest_notes.state(stopped) == "stopped"
    assert iroh.rooms[row["room"]].deleted and mirror.rooms[row["room"]].deleted
    assert room_ops(sidecar, "room_stop") == [{"op": "room_stop", "room": row["room"]}]
    assert guest_notes.bridges_of(ctx, sid) == []
    # No room left to serve: the sidecar is stopped.
    assert not ctx.rooms_link.active


async def test_end_ends_both_and_keeps_serving_until_expiry(ctx, iroh, mirror, sidecar):
    sid = meeting(ctx)
    row, _ = await started(ctx, iroh, sid)
    ended = await guest_notes.end(ctx, sid, by="Corey")
    assert guest_notes.state(ended) == "ended"
    assert iroh.rooms[row["room"]].ended and mirror.rooms[row["room"]].ended
    assert guest_notes.bridges_of(ctx, sid) == []
    # Readable over iroh until it expires.
    assert ctx.rooms_link.active and room_ops(sidecar, "room_stop") == []
    assert sid in ctx.guest_bridges.expiry


async def test_new_link_on_an_iroh_room(ctx, iroh, sidecar):
    sid = meeting(ctx)
    row, _ = await started(ctx, iroh, sid)
    fresh = await guest_notes.new_link(ctx, sid, by="Corey")
    assert fresh["room"] != row["room"] and fresh["transport"] == "iroh"
    assert room_ops(sidecar, "room_stop") == [{"op": "room_stop", "room": row["room"]}]
    assert [o["room"] for o in room_ops(sidecar, "room_start")] == [row["room"], fresh["room"]]
    assert len(sidecar.rooms_starts) == 1  # the sidecar was kept running in between


async def test_resume_serves_the_room_again_with_its_relay(ctx, iroh, mirror, sidecar):
    sid = meeting(ctx)
    row, _ = await started(ctx, iroh, sid)
    await guest_notes.shutdown(ctx)
    await ctx.rooms_link.stop()  # the backend went away, and its sidecar with it
    assert await guest_notes.resume(ctx) == 1
    assert room_ops(sidecar, "room_start")[-1] == {
        "op": "room_start",
        "room": row["room"],
        "relay": N0_RELAY,
    }
    assert len(sidecar.rooms_starts) == 2
    for kind in (guest_notes.PRIMARY, guest_notes.MIRROR):
        b = guest_notes.bridge_for(ctx, sid, kind)
        assert b is not None
        await until(lambda b=b: b.connected.is_set())
    assert ctx.repo.guest_room(sid)["endpoint_id"] == ENDPOINT


async def test_a_sidecar_restart_serves_the_live_rooms_again(ctx, iroh, sidecar):
    sid = meeting(ctx)
    row, _ = await started(ctx, iroh, sid)
    other = meeting(ctx)
    await guest_notes.start(ctx, other)
    before = len(room_ops(sidecar, "room_start"))
    ctx.rooms_link._proc.kill()  # a crash: it comes back serving nothing
    await until(lambda: len(room_ops(sidecar, "room_start")) >= before + 2)
    again = room_ops(sidecar, "room_start")[before:]
    rooms = {row["room"], ctx.repo.guest_room(other)["room"]}
    assert {o["room"] for o in again} == rooms and all(o["relay"] == N0_RELAY for o in again)
    assert len(sidecar.rooms_starts) == 2


async def test_iroh_refused_falls_back_to_the_publish_host_in_auto(ctx, iroh, settings, sidecar):
    h = await FakeRoomHost().start()
    try:
        ctx.http_transport = routed(iroh, h)
        settings.publish_host = h.url
        sidecar.config["fail"] = {"room_start": "no relay reachable"}
        sidecar.sync()
        sid = meeting(ctx)
        row = await guest_notes.start(ctx, sid)
        assert row["transport"] == "host" and row["host_url"] == h.url
        assert row["endpoint_id"] == "" and row["mirror_host_url"] == ""
        assert guest_notes.link(row) == f"{h.url}/n/{row['room']}#{gf.key_to_fragment(row['key'])}"
        assert h.room()[0] == row["room"]
        # the half-made loopback room went again
        assert iroh.rooms and all(r.deleted for r in iroh.rooms.values())
        assert not ctx.rooms_link.active  # nothing to serve
        # iroh asked for explicitly: the refusal is the answer.
        settings.guest_transport = "iroh"
        with pytest.raises(guest_notes.GuestNotesError, match="no relay reachable"):
            await guest_notes.new_link(ctx, sid)
    finally:
        await guest_notes.shutdown(ctx)
        await h.close()


async def test_no_iroh_and_no_host_is_unavailable(ctx, iroh, settings, sidecar):
    sidecar.config["fail"] = {"room_start": "no relay reachable"}
    sidecar.sync()
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="no publish host"):
        await guest_notes.start(ctx, meeting(ctx))
    assert not ctx.rooms_link.active


async def test_a_sidecar_that_cannot_start(ctx, iroh, settings, sidecar):
    del sidecar.config["listen"]  # it dies before its ready line
    sidecar.sync()
    settings.guest_transport = "iroh"
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="Guest rooms cannot run"):
        await guest_notes.plan(ctx)
    await ctx.rooms_link.stop()
    settings.guest_transport = "auto"
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="no publish host is set"):
        await guest_notes.plan(ctx)
    await ctx.rooms_link.stop()
    settings.publish_host = "https://host.example"
    p = await guest_notes.plan(ctx)
    assert (p.transport, p.host_url, p.mirror_host) == ("host", "https://host.example", "")
    assert p.page_origin == guest_notes.GUEST_PAGE_ORIGIN
    # A tunnel fronts the sidecar's loopback host: no sidecar, no tunnel room.
    settings.guest_transport = "tunnel"
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="Guest rooms cannot run"):
        await guest_notes.plan(ctx)
    await ctx.rooms_link.stop()


# ---- tunnel rooms (phase 10, task 20) ---------------------------------------------------------


class FakeTunnel:
    kind = "cloudflare"

    def __init__(self, url: str, port: int):
        self.url, self.port = url, port
        self.up = True
        self.stopped = False

    def alive(self) -> bool:
        return self.up and not self.stopped

    async def stop(self) -> None:
        self.stopped = True


class Tunnels:
    """`tunnel.open_tunnel` played: each open is a FakeTunnel on the next address (or fails)."""

    def __init__(self) -> None:
        self.opened: list[FakeTunnel] = []
        self.fail = ""
        self.urls = iter(f"https://quiet-{i}.trycloudflare.com" for i in range(1, 100))

    async def __call__(self, app, port):
        from mnemosyne.services.tunnel import TunnelError

        if self.fail:
            raise TunnelError(self.fail)
        t = FakeTunnel(next(self.urls), port)
        self.opened.append(t)
        return t

    @property
    def open(self) -> list[FakeTunnel]:
        return [t for t in self.opened if not t.stopped]


@pytest.fixture
def tunnels(monkeypatch, iroh) -> Tunnels:
    from mnemosyne.services import tunnel

    fake = Tunnels()
    monkeypatch.setattr(tunnel, "open_tunnel", fake)
    return fake


async def test_a_tunnel_room_is_reached_on_the_tunnel_and_over_iroh(
    ctx, settings, iroh, tunnels, sidecar
):
    settings.guest_transport = "tunnel"
    sid = meeting(ctx)
    row, bridge = await started(ctx, iroh, sid)
    assert row["transport"] == "tunnel" and row["public_url"] == "https://quiet-1.trycloudflare.com"
    assert row["endpoint_id"] == ENDPOINT and row["relay"] == N0_RELAY
    assert row["host_url"] == iroh.url == ctx.rooms_link.base_url
    # One tunnel, to the loopback host's port; the room is made there and served over iroh.
    assert [t.port for t in tunnels.opened] == [int(iroh.url.rsplit(":", 1)[1])]
    assert room_ops(sidecar, "room_start") == [{"op": "room_start", "room": row["room"]}]
    assert iroh.calls[0][:2] == ("PUT", f"/live/{row['room']}")
    key = gf.key_to_fragment(row["key"])
    assert guest_notes.link(row) == (
        f"https://quiet-1.trycloudflare.com/n/{row['room']}"
        f"#2.{row['room']}.{key}.{ENDPOINT}.use1-1.relay.n0.iroh.link."
    )
    assert guest_notes.served_from(row) == "https://quiet-1.trycloudflare.com/n/"
    assert [e["kind"] for e in ctx.repo.events(sid)].count("guest_notes_started") == 1
    # Mirrored on a configured publish host as an iroh room is; the link stays on the tunnel.
    assert guest_notes.bridge_for(ctx, sid, guest_notes.MIRROR) is None
    assert bridge.target.transport == "tunnel"
    ana = await guest(iroh, row)
    await ana.close()


async def test_a_tunnel_room_that_cannot_be_served_over_iroh_has_a_format_1_link(
    ctx, settings, iroh, tunnels, sidecar
):
    settings.guest_transport = "tunnel"
    sidecar.config["fail"] = {"room_start": "no relay reachable"}
    sidecar.sync()
    row = await guest_notes.start(ctx, meeting(ctx))
    assert row["transport"] == "tunnel" and row["endpoint_id"] == "" and row["relay"] == ""
    assert guest_notes.link(row) == (
        f"https://quiet-1.trycloudflare.com/n/{row['room']}#{gf.key_to_fragment(row['key'])}"
    )


async def test_one_tunnel_serves_every_tunnel_room_and_closes_with_the_last(
    ctx, settings, iroh, tunnels, sidecar
):
    settings.guest_transport = "tunnel"
    one, two = meeting(ctx), meeting(ctx)
    a = await guest_notes.start(ctx, one)
    b = await guest_notes.start(ctx, two)
    assert len(tunnels.opened) == 1 and a["public_url"] == b["public_url"]
    await guest_notes.stop(ctx, one)
    assert len(tunnels.open) == 1 and ctx.rooms_link.active
    await guest_notes.stop(ctx, two)
    assert tunnels.open == [] and not ctx.rooms_link.active
    # A new room opens a new tunnel.
    c = await guest_notes.start(ctx, one)
    assert len(tunnels.opened) == 2 and c["public_url"] == "https://quiet-2.trycloudflare.com"


async def test_an_ended_tunnel_room_keeps_the_tunnel_until_it_expires(
    ctx, settings, iroh, tunnels, sidecar
):
    settings.guest_transport = "tunnel"
    sid = meeting(ctx)
    await guest_notes.start(ctx, sid)
    await guest_notes.end(ctx, sid)
    assert len(tunnels.open) == 1  # readable until it expires
    # Expired: the tunnel and the sidecar go.
    ctx.repo.set_guest_room(sid, expires_at=guest_notes._iso(guest_notes._now()))
    await guest_notes._maybe_stop_rooms(ctx)
    assert tunnels.open == [] and not ctx.rooms_link.active


async def test_auto_tries_the_tunnel_when_iroh_fails_then_the_host(
    ctx, settings, iroh, tunnels, sidecar
):
    sidecar.config["fail"] = {"room_start": "no relay reachable"}
    sidecar.sync()
    sid = meeting(ctx)
    # iroh fails, the tunnel opens: a tunnel room (no endpoint), not a host room.
    row = await guest_notes.start(ctx, sid)
    assert row["transport"] == "tunnel" and row["endpoint_id"] == ""
    await guest_notes.stop(ctx, sid)
    assert tunnels.open == []
    # The tunnel fails too: the configured host.
    h = await FakeRoomHost().start()
    try:
        ctx.http_transport = routed(iroh, h)
        settings.publish_host = h.url
        tunnels.fail = "cloudflared stopped (code 1)"
        row = await guest_notes.start(ctx, sid)
        assert row["transport"] == "host" and row["host_url"] == h.url
        assert row["public_url"] == "" and not ctx.rooms_link.active
        await guest_notes.stop(ctx, sid)
    finally:
        await h.close()
    # And with no host: unavailable, with both reasons.
    settings.publish_host = ""
    with pytest.raises(guest_notes.GuestNotesUnavailable) as err:
        await guest_notes.start(ctx, sid)
    text = str(err.value)
    assert "no relay reachable" in text and "cloudflared stopped" in text
    assert "no publish host" in text
    assert not ctx.rooms_link.active and tunnels.open == []
    # Asked for explicitly, the tunnel's reason is the answer.
    settings.guest_transport = "tunnel"
    with pytest.raises(guest_notes.GuestNotesUnavailable, match="cloudflared stopped"):
        await guest_notes.start(ctx, sid)
    assert ctx.repo.guest_room(sid)["deleted_at"]


async def test_resume_opens_the_tunnel_again_and_logs_a_changed_link(
    ctx, settings, iroh, tunnels, sidecar
):
    settings.guest_transport = "tunnel"
    sid = meeting(ctx)
    row = await guest_notes.start(ctx, sid)
    assert row["public_url"] == "https://quiet-1.trycloudflare.com"
    await guest_notes.shutdown(ctx)  # the backend stops: the tunnel goes with it
    assert tunnels.open == []
    await ctx.rooms_link.stop()
    assert await guest_notes.resume(ctx) == 1
    assert len(tunnels.open) == 1
    row = ctx.repo.guest_room(sid)
    assert row["public_url"] == "https://quiet-2.trycloudflare.com"
    assert [e["kind"] for e in ctx.repo.events(sid)].count("guest_notes_link_changed") == 1
    assert guest_notes.link(row).startswith("https://quiet-2.trycloudflare.com/n/")
    # Same address again (a Funnel's): nothing to tell.
    await guest_notes.shutdown(ctx)
    await ctx.rooms_link.stop()
    tunnels.urls = iter(["https://quiet-2.trycloudflare.com"])
    assert await guest_notes.resume(ctx) == 1
    assert [e["kind"] for e in ctx.repo.events(sid)].count("guest_notes_link_changed") == 1


async def test_a_tunnel_that_cannot_reopen_at_startup_is_logged_not_raised(
    ctx, settings, iroh, tunnels, sidecar
):
    settings.guest_transport = "tunnel"
    sid = meeting(ctx)
    await guest_notes.start(ctx, sid)
    await guest_notes.shutdown(ctx)
    await ctx.rooms_link.stop()
    tunnels.fail = "no internet"
    assert await guest_notes.resume(ctx) == 1  # the bridge runs; guests on iroh still get in
    assert [
        e["detail"].get("error")
        for e in ctx.repo.events(sid)
        if e["kind"] == "guest_notes_tunnel_failed"
    ] == ["Could not open a public tunnel: no internet"]
    assert ctx.repo.guest_room(sid)["public_url"] == "https://quiet-1.trycloudflare.com"


async def test_a_tunnel_whose_process_died_is_reopened_once(
    ctx, settings, iroh, tunnels, sidecar, monkeypatch
):
    monkeypatch.setattr(guest_notes, "TUNNEL_CHECK", 0.01)
    monkeypatch.setattr(guest_notes, "TUNNEL_RETRY", 0.01)
    settings.guest_transport = "tunnel"
    sid = meeting(ctx)
    await guest_notes.start(ctx, sid)
    tunnels.opened[0].up = False  # cloudflared died
    await until(lambda: len(tunnels.opened) == 2)
    await until(
        lambda: ctx.repo.guest_room(sid)["public_url"] == "https://quiet-2.trycloudflare.com"
    )
    assert tunnels.opened[0].stopped and len(tunnels.open) == 1
    assert [e["kind"] for e in ctx.repo.events(sid)].count("guest_notes_link_changed") == 1
    # It dies again and cannot be reopened: given up, nothing raised, the row keeps its address.
    tunnels.fail = "no internet"
    tunnels.opened[1].up = False
    await until(lambda: tunnels.open == [])
    await asyncio.sleep(0.05)
    assert len(tunnels.opened) == 2
    assert ctx.repo.guest_room(sid)["public_url"] == "https://quiet-2.trycloudflare.com"
    # The next start of the room brings the tunnel back.
    tunnels.fail = ""
    await guest_notes.start(ctx, sid)
    assert len(tunnels.open) == 1


async def test_the_computer_stays_awake_while_an_iroh_or_tunnel_room_is_live(
    ctx, settings, iroh, tunnels, sidecar
):
    from mnemosyne.services import awake

    assert not guest_notes.needs_awake(ctx) and awake.reason(ctx) == ""
    for mode in ("auto", "tunnel"):
        settings.guest_transport = mode
        sid = meeting(ctx)
        await guest_notes.start(ctx, sid)
        assert guest_notes.needs_awake(ctx) and awake.reason(ctx) == "Guest notes are open"
        await guest_notes.end(ctx, sid)  # ended: readable, but nobody types into it
        assert not guest_notes.needs_awake(ctx) and awake.reason(ctx) == ""
    sid = meeting(ctx)
    await guest_notes.start(ctx, sid)
    assert awake.reason(ctx) == "Guest notes are open"
    await guest_notes.stop(ctx, sid)
    assert awake.reason(ctx) == ""


async def test_a_host_room_does_not_keep_the_computer_awake(ctx, host):
    from mnemosyne.services import awake

    sid = meeting(ctx)
    await started(ctx, host, sid)
    assert not guest_notes.needs_awake(ctx) and awake.reason(ctx) == ""
