"""The connection loop alone (services/room_connection.py), against tests/fake_guest_room.py's
FakeRoomHost: join, acknowledgements in order, a lagged rejoin at once, a full room retried, a
full log handed to the hook, the proxy flag on loopback. The two bridges' own behaviour is
pinned by test_guest_notes.py and test_live_publications.py."""

import asyncio
import contextlib
import json
import time
from collections import deque

import pytest
from websockets.asyncio import client as ws_client

from mnemosyne.services.room_connection import (
    CLOSE_BAD_TOKEN,
    CLOSE_ENDED,
    CLOSE_FULL,
    CLOSE_LAGGED,
    CLOSE_TOO_LARGE,
    RETRY_BACKOFF,
    STOP,
    CloseAction,
    Decision,
    ReadAction,
    RoomConnection,
    RoomHooks,
    WelcomeAction,
    is_loopback,
)
from tests.fake_guest_room import T_NOTES, T_PRESENCE, FakeRoom, FakeRoomHost

pytestmark = pytest.mark.anyio

ROOM, ROOM_TOKEN = "room1", "rt"


async def until(check, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
async def host():
    h = await FakeRoomHost().start()
    h.rooms[ROOM] = FakeRoom("pub", ROOM_TOKEN, "2099-01-01T00:00:00Z")
    yield h
    await h.close()


class Hooks(RoomHooks):
    """Items are (tag, bytes); the log of what the loop told the hooks is `events`."""

    def __init__(self, host: FakeRoomHost, token: str = ROOM_TOKEN) -> None:
        self.host = host
        self.token = token
        self.seq = 0
        self.events: list = []
        self.frames: list[tuple[int, bytes]] = []
        self.presence: list[bytes] = []
        self.acked: list = []
        self.closes: list[tuple] = []
        self.on_closed_script: list[Decision] = []
        self.welcome_action = WelcomeAction.CONTINUE
        self.conn: RoomConnection | None = None
        self.outq: asyncio.Queue = asyncio.Queue()

    def url(self) -> str:
        return f"{self.host.url.replace('http', 'ws', 1)}/live/{ROOM}"

    def join_message(self) -> dict:
        return {"type": "join", "token": self.token, "since": self.seq}

    async def on_welcome(self, welcome: dict) -> WelcomeAction:
        self.events.append(("welcome", welcome["present"]))
        return self.welcome_action

    def on_connected(self) -> None:
        self.events.append("connected")

    def prepare(self, item) -> bytes:
        tag, env = item
        self.conn.unacked.append(env)
        return bytes([tag]) + env

    def resend(self, unacked: list) -> list[bytes]:
        return [bytes([T_NOTES]) + env for env in unacked]

    def on_ack(self, done) -> None:
        self.acked.append(done)

    def advance(self, seq) -> None:
        if isinstance(seq, int) and seq > self.seq:
            self.seq = seq

    async def on_presence(self, payload: bytes) -> None:
        self.presence.append(payload)

    async def on_frame(self, seq: int, envelope: bytes) -> None:
        self.frames.append((seq, envelope))
        self.advance(seq)

    def on_disconnected(self) -> None:
        self.events.append("disconnected")

    def on_unreachable(self, error: BaseException) -> None:
        self.events.append(("unreachable", type(error).__name__))

    async def on_closed(self, code, status, last_error, attempt, unacked) -> Decision:
        self.closes.append((code, status, last_error, attempt))
        return self.on_closed_script.pop(0) if self.on_closed_script else STOP


@contextlib.asynccontextmanager
async def spawned(conn: RoomConnection):
    task = asyncio.create_task(conn.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def make(hooks: Hooks, backoff=(0.01,)) -> RoomConnection:
    conn = RoomConnection(
        hooks,
        outq=hooks.outq,
        max_message=1024 * 1024,
        backoff=backoff,
        join_timeout=2.0,
        label="Test room",
    )
    hooks.conn = conn
    return conn


@pytest.fixture
async def started(host):
    hooks = Hooks(host)
    conn = make(hooks)
    task = asyncio.create_task(conn.run())
    await until(lambda: conn.connected.is_set())
    yield hooks, conn, host.rooms[ROOM]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_join_sends_the_message_and_hears_the_welcome(started):
    hooks, conn, room = started
    assert hooks.host.joins == [(ROOM, 0)]
    assert hooks.events == [("welcome", 1), "connected"]
    assert conn.joins == 1 and conn.ws is not None


async def test_frames_are_acknowledged_in_order_and_advance_the_seq(started):
    hooks, conn, room = started
    for n in (b"a", b"b", b"c"):
        hooks.outq.put_nowait((T_NOTES, n))
    await until(lambda: hooks.seq == 3 and not conn.unacked)
    assert [e for _, e in room.log] == [b"a", b"b", b"c"]
    assert hooks.acked == [b"a", b"b", b"c"]  # what the loop popped, oldest first


async def test_frames_from_the_room_come_with_their_seq_and_presence_without(started):
    hooks, conn, room = started
    ws = next(iter(room.clients))
    await ws.send(bytes([T_NOTES]) + (7).to_bytes(8, "big") + b"env")
    await ws.send(bytes([T_PRESENCE]) + b"here")
    await ws.send(bytes([0x03]) + (1).to_bytes(8, "big") + b"snap")  # ours: ignored
    await ws.send(b"")
    await until(lambda: hooks.frames and hooks.presence)
    assert hooks.frames == [(7, b"env")] and hooks.presence == [b"here"]
    assert hooks.seq == 7


async def test_a_lagged_close_rejoins_at_once_with_the_seq(started):
    hooks, conn, room = started
    hooks.on_closed_script = [Decision(CloseAction.RETRY_NOW, 0), STOP]
    hooks.seq = 5
    await next(iter(room.clients)).close(CLOSE_LAGGED)
    await until(lambda: len(hooks.host.joins) == 2)
    assert hooks.host.joins[-1] == (ROOM, 5)
    assert hooks.closes[0][0] == CLOSE_LAGGED
    await until(lambda: conn.connected.is_set())


async def test_a_full_room_is_retried_after_the_backoff(host):
    host.rooms[ROOM].full = True
    hooks = Hooks(host)
    hooks.on_closed_script = [RETRY_BACKOFF, RETRY_BACKOFF]
    conn = make(hooks)
    task = asyncio.create_task(conn.run())
    await until(lambda: len(hooks.closes) >= 2)
    assert [c[0] for c in hooks.closes[:2]] == [CLOSE_FULL, CLOSE_FULL]
    assert [c[3] for c in hooks.closes[:2]] == [0, 1]  # the attempt counts up
    host.rooms[ROOM].full = False
    await until(lambda: conn.connected.is_set())
    assert conn.joins == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_full_log_is_handed_to_the_hook_with_the_hosts_error(started):
    hooks, conn, room = started
    hooks.on_closed_script = [RETRY_BACKOFF, STOP]
    ws = next(iter(room.clients))
    await ws.send(json.dumps({"type": "error", "code": "log_full"}))
    await ws.close(CLOSE_TOO_LARGE)
    await until(lambda: len(hooks.host.joins) == 2)
    assert hooks.closes[0] == (CLOSE_TOO_LARGE, None, "log_full", 0)
    # The next attempt starts with no error remembered.
    await until(lambda: conn.connected.is_set())
    await next(iter(room.clients)).close(CLOSE_LAGGED)
    await until(lambda: hooks.closes[1:])
    assert hooks.closes[1][2] == ""


async def test_unacknowledged_items_are_sent_again_first_and_the_hook_may_edit_them(host):
    hooks = Hooks(host)
    conn = make(hooks)
    conn.unacked.extend([b"left-1", b"left-2"])
    hooks.outq.put_nowait((T_NOTES, b"new"))
    task = asyncio.create_task(conn.run())
    await until(lambda: len(host.rooms[ROOM].log) == 3)
    assert [e for _, e in host.rooms[ROOM].log] == [b"left-1", b"left-2", b"new"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_an_ended_welcome_goes_to_the_hook_and_leaves_as_closed_ended(host):
    host.rooms[ROOM].ended = True
    hooks = Hooks(host)
    hooks.welcome_action = WelcomeAction.LEAVE_ROOM
    conn = make(hooks)
    await asyncio.wait_for(conn.run(), 5)
    assert hooks.closes == [(CLOSE_ENDED, None, "", 0)]
    assert hooks.events == [("welcome", 1), "disconnected"]  # never connected
    assert conn.joins == 0


async def test_stop_from_the_welcome_ends_the_loop_without_on_closed(host):
    hooks = Hooks(host)
    hooks.welcome_action = WelcomeAction.STOP
    await asyncio.wait_for(make(hooks).run(), 5)
    assert hooks.closes == [] and hooks.events == [("welcome", 1), "disconnected"]


async def test_an_ended_message_can_leave_the_room_or_stop(started):
    hooks, conn, room = started

    async def on_text(msg):
        return ReadAction.STOP if msg.get("type") == "ended" else None

    hooks.on_text = on_text
    ws = next(iter(room.clients))
    await ws.send(json.dumps({"type": "ended"}))
    await until(lambda: "disconnected" in hooks.events)
    assert hooks.closes == [] and not conn.connected.is_set()


async def test_count_and_ping_are_answered_by_the_loop(started):
    hooks, conn, room = started
    counts = []
    hooks.on_count = counts.append
    ws = next(iter(room.clients))
    await ws.send(json.dumps({"type": "count", "present": 4}))
    await ws.send("ping")
    await until(lambda: counts == [4])


async def test_a_refused_token_closes_4401(host):
    hooks = Hooks(host, token="wrong")
    await asyncio.wait_for(make(hooks).run(), 5)
    assert [c[0] for c in hooks.closes] == [CLOSE_BAD_TOKEN]


async def test_an_unreachable_room_goes_to_the_hook_and_is_retried(host):
    hooks = Hooks(host)
    await host.close()
    hooks.on_closed_script = [RETRY_BACKOFF, STOP]
    await asyncio.wait_for(make(hooks).run(), 5)
    assert [c[:2] for c in hooks.closes] == [(None, None), (None, None)]
    assert ("unreachable", "ConnectionRefusedError") in hooks.events


async def test_before_connect_can_hold_the_dial_and_set_the_attempt(host):
    hooks = Hooks(host)
    seen = []

    async def before_connect(attempt):
        seen.append(attempt)
        return attempt + 1 if len(seen) < 3 else None

    hooks.before_connect = before_connect
    conn = make(hooks)
    async with spawned(conn):
        await until(lambda: conn.connected.is_set())
    assert seen == [0, 1, 2] and len(host.joins) == 1


async def test_stop_requested_ends_the_loop_after_an_attempt(host):
    hooks = Hooks(host)
    hooks.stop_requested = lambda: True
    conn = make(hooks)
    async with spawned(conn) as task:
        await until(lambda: conn.connected.is_set())
        await next(iter(host.rooms[ROOM].clients)).close(CLOSE_LAGGED)
        await asyncio.wait_for(task, 5)
    assert hooks.closes == [] and host.joins == [(ROOM, 0)]


async def test_the_backoff_table_is_walked_and_its_last_delay_repeats(host, monkeypatch):
    hooks = Hooks(host)
    await host.close()
    hooks.on_closed_script = [RETRY_BACKOFF] * 4 + [STOP]
    delays = []
    real_sleep = asyncio.sleep

    async def sleep(d, *a):
        delays.append(d)
        await real_sleep(0)

    conn = make(hooks, backoff=lambda: (0.1, 0.2))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await asyncio.wait_for(conn.run(), 5)
    assert delays == [0.1, 0.2, 0.2, 0.2]
    assert [c[3] for c in hooks.closes] == [0, 1, 2, 3, 4]


async def test_loopback_dials_without_a_proxy_and_others_with_one(host, monkeypatch):
    seen = {}
    real = ws_client.connect

    def spy(url, **kw):
        seen.update(kw)
        return real(url, **kw)

    monkeypatch.setattr(ws_client, "connect", spy)
    conn = make(Hooks(host))
    async with spawned(conn):
        await until(lambda: conn.connected.is_set())
    assert seen["proxy"] is None and seen["compression"] is None
    assert seen["max_size"] == 1024 * 1024 and seen["open_timeout"] == 2.0
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("notes.example.org") and not is_loopback("10.0.0.2")


async def test_on_closed_is_handed_the_connections_unacked_deque(host):
    hooks = Hooks(host)
    got = []

    async def on_closed(code, status, last_error, attempt, unacked):
        got.append(unacked)
        return STOP

    hooks.on_closed = on_closed
    conn = make(hooks)
    async with spawned(conn) as task:
        await until(lambda: conn.connected.is_set())
        await next(iter(host.rooms[ROOM].clients)).close(CLOSE_LAGGED)
        await asyncio.wait_for(task, 5)
    assert got == [conn.unacked] and isinstance(got[0], deque)
