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
from tests.fake_guest_room import T_NOTES, T_PRESENCE, FakeRoomHost, GuestClient
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
    settings.local_speaker_name = "Corey"
    yield h
    await guest_notes.shutdown(ctx)
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
    assert sent[-1] == {"type": "guests", "present": ["Ana (guest)"], "typing": ["Ana (guest)"]}
    assert guest_notes.guests_present(ctx, sid) == ["Ana (guest)"]
    # The owner's heartbeat goes to the room; a guest silent for too long has gone.
    await bridge.tick(now=time.monotonic() + guest_notes.PRESENCE_TTL + 1)
    assert sent[-1] == {"type": "guests", "present": [], "typing": []}
    beat = await until(lambda: ana.binary(T_PRESENCE))
    kind, obj = gf.unseal_json(row["key"], row["room"], beat[0][1])
    assert obj == {"gid": "", "name": "Corey", "typing": False}
    await ana.close()


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


async def test_a_room_deleted_while_away_is_refused_before_the_upgrade(ctx, host):
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
