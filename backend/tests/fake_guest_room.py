"""A stand-in publish host for guest notes rooms (phase 9): the room protocol of
services/guest_notes.py, in process, on 127.0.0.1 only.

`FakeRoomHost` keeps a table of rooms (publisher token, room token, expiry, seq, log, snapshot,
ended/deleted) shared by two faces:

- `handler`, an httpx.MockTransport handler (put it on `ctx.http_transport`) for the HTTP routes:
  `PUT /live/{room}` (X-Room-Token, X-Room-Expires as unix seconds or zoned ISO; 201 new, 200
  existing), `PUT /live/{room}/snapshot` (X-Room-Since required, 409 since_ahead or
  snapshot_stale; the log up to it is dropped), `POST /live/{room}/end` (optional
  X-Room-Expires; connected clients hear `{"type":"ended"}`), `DELETE /live/{room}` (clients
  closed with 4410; 410 on everything afterwards). Every
  request is in `calls` as (method, path, headers, body).
- a real `websockets` server (`await host.start()`, `host.url`) for `GET /live/{room}`: the join
  message, `welcome`, the snapshot (0x03) when `since` is older, the log after `since` (0x01),
  then live: a client's 0x01 is given the next seq, logged, relayed to the OTHER clients and
  acknowledged to the sender; 0x02 (presence) is relayed, not logged; "ping" gets "pong". Close
  codes 4401 (bad token), 4403 (frames to an ended room), 4410 (no such room or deleted),
  4429 (`room.full`: at its connection cap). Like the real host, an unknown, deleted or full room
  is upgraded and then closed at once (a browser cannot see an HTTP status); an empty or
  unknown frame gets `{"type":"error","code":"bad_frame"}`. `joins` lists (room, since).

`GuestClient` is a guest page's socket for tests: it records everything it receives.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

T_NOTES, T_PRESENCE, T_SNAPSHOT = 0x01, 0x02, 0x03


@dataclass
class FakeRoom:
    token: str
    room_token: str
    expires: str
    seq: int = 0
    log: list[tuple[int, bytes]] = field(default_factory=list)
    snapshot: bytes | None = None
    snapshot_seq: int = 0
    ended: bool = False
    ended_at: str | None = None
    deleted: bool = False
    full: bool = False  # refused with 4429, as a room at its connection cap is
    clients: set = field(default_factory=set)


class FakeRoomHost:
    def __init__(self) -> None:
        self.rooms: dict[str, FakeRoom] = {}
        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.joins: list[tuple[str, int]] = []
        self.status: dict[tuple[str, str], int] = {}  # (method, suffix) -> forced status
        self._server = None
        self.url = ""

    # ---- lifecycle --------------------------------------------------------------------------

    async def start(self) -> FakeRoomHost:
        self._server = await serve(
            self._ws,
            "127.0.0.1",
            0,
            compression=None,
            max_size=None,
        )
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ---- HTTP -------------------------------------------------------------------------------

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        headers = {k.lower(): v for k, v in request.headers.items()}
        body = request.content
        self.calls.append((request.method, path, headers, body))
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "live":
            return httpx.Response(404, json={"error": "not_found"})
        room_id, rest = parts[1], "/".join(parts[2:])
        forced = self.status.get((request.method, rest))
        if forced:
            return httpx.Response(forced, json={"error": "forced"})
        auth = headers.get("authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        room = self.rooms.get(room_id)
        if room is not None and room.deleted:
            return httpx.Response(410, json={"error": "gone"})
        if request.method == "PUT" and rest == "":
            expires = headers.get("x-room-expires", "")
            if not expires.isdigit() and not (expires.endswith("Z") or "+" in expires[10:]):
                return httpx.Response(400, json={"error": "bad_expires"})
            if not token or "x-room-token" not in headers:
                return httpx.Response(400, json={"error": "bad_request"})
            created = room is None
            if created:
                room = FakeRoom(token, headers["x-room-token"], expires)
                self.rooms[room_id] = room
            elif token != room.token:
                return httpx.Response(403, json={"error": "wrong_token"})
            return httpx.Response(
                201 if created else 200,
                json={
                    "created": created,
                    "expires": room.expires,
                    "ended_at": room.ended_at,
                    "last_seq": room.seq,
                    "snapshot_seq": room.snapshot_seq if room.snapshot is not None else None,
                },
            )
        if room is None:
            return httpx.Response(404, json={"error": "not_found"})
        if token != room.token:
            return httpx.Response(403, json={"error": "wrong_token"})
        if request.method == "PUT" and rest == "snapshot":
            if "x-room-since" not in headers:
                return httpx.Response(400, json={"error": "bad_since"})
            since = int(headers["x-room-since"])
            if since > room.seq:
                return httpx.Response(409, json={"error": "since_ahead"})
            if since < room.snapshot_seq:
                return httpx.Response(409, json={"error": "snapshot_stale"})
            room.snapshot, room.snapshot_seq = body, since
            room.log = [(s, e) for s, e in room.log if s > since]
            return httpx.Response(204)
        if request.method == "POST" and rest == "end":
            room.ended = True
            room.ended_at = room.ended_at or "2026-10-24T12:00:00Z"
            room.expires = headers.get("x-room-expires", room.expires)
            ended = {"type": "ended", "ended_at": room.ended_at, "expires": room.expires}
            for ws in list(room.clients):
                await _send(ws, json.dumps(ended))
            return httpx.Response(200, json={"ended_at": room.ended_at, "expires": room.expires})
        if request.method == "DELETE" and rest == "":
            room.deleted = True
            for ws in list(room.clients):
                await ws.close(4410)
            return httpx.Response(204)
        return httpx.Response(405)

    # ---- WebSocket --------------------------------------------------------------------------

    async def _ws(self, ws) -> None:
        room_id = ws.request.path.rstrip("/").rsplit("/", 1)[-1]
        room = self.rooms.get(room_id)
        if room is None or room.deleted:
            await ws.close(4410, "gone")
            return
        if room.full:
            await ws.close(4429, "room_full")
            return
        try:
            hello = json.loads(await ws.recv())
        except (ConnectionClosed, ValueError):
            return
        if not isinstance(hello, dict) or hello.get("token") != room.room_token:
            await ws.close(4401)
            return
        since = int(hello.get("since") or 0)
        self.joins.append((room_id, since))
        await ws.send(
            json.dumps(
                {
                    "type": "welcome",
                    "seq": room.seq,
                    "snapshot_seq": room.snapshot_seq if room.snapshot is not None else None,
                    "ended": room.ended,
                    "ended_at": room.ended_at,
                    "expires": room.expires,
                    "present": len(room.clients) + 1,
                }
            )
        )
        if room.snapshot is not None and since < room.snapshot_seq:
            await ws.send(
                bytes([T_SNAPSHOT]) + room.snapshot_seq.to_bytes(8, "big") + room.snapshot
            )
        for seq, env in room.log:
            if seq > since:
                await ws.send(bytes([T_NOTES]) + seq.to_bytes(8, "big") + env)
        room.clients.add(ws)
        await self._count(room)
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    if msg == "ping":
                        await ws.send("pong")
                    continue
                if not msg or msg[0] not in (T_NOTES, T_PRESENCE):
                    await ws.send(json.dumps({"type": "error", "code": "bad_frame"}))
                    continue
                if msg[0] == T_NOTES:
                    if room.ended:
                        await ws.close(4403)
                        return
                    room.seq += 1
                    seq = room.seq
                    env = bytes(msg[1:])
                    room.log.append((seq, env))
                    out = bytes([T_NOTES]) + seq.to_bytes(8, "big") + env
                    # The ack goes out as the frame passes the relay, in seq order.
                    for other in list(room.clients):
                        if other is not ws:
                            await _send(other, out)
                        else:
                            await _send(ws, json.dumps({"type": "ack", "seq": seq}))
                elif msg[0] == T_PRESENCE:
                    for other in list(room.clients):
                        if other is not ws:
                            await _send(other, bytes(msg))
        except ConnectionClosed:
            pass
        finally:
            room.clients.discard(ws)
            await self._count(room)

    async def _count(self, room: FakeRoom) -> None:
        msg = json.dumps({"type": "count", "present": len(room.clients)})
        for ws in list(room.clients):
            await _send(ws, msg)

    # ---- for assertions ---------------------------------------------------------------------

    def room(self) -> tuple[str, FakeRoom]:
        """The one room there is (tests make one)."""
        live = [(k, r) for k, r in self.rooms.items() if not r.deleted]
        assert len(live) == 1, self.rooms
        return live[0]


async def _send(ws, data) -> None:
    try:
        await ws.send(data)
    except ConnectionClosed:
        pass


class GuestClient:
    """A guest page's connection: `received` holds every message (str or bytes) in order."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.received: list = []
        self.welcome: dict = {}
        self.closed_code: int | None = None
        self._reader: asyncio.Task | None = None

    @classmethod
    async def join(cls, host: FakeRoomHost, room: str, room_token: str, since: int = 0):
        ws = await connect(f"{host.url.replace('http', 'ws', 1)}/live/{room}", proxy=None)
        await ws.send(json.dumps({"type": "join", "token": room_token, "since": since}))
        client = cls(ws)
        client.welcome = json.loads(await ws.recv())
        client._reader = asyncio.create_task(client._read())
        return client

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                self.received.append(msg)
        except ConnectionClosed as e:
            self.closed_code = e.rcvd.code if e.rcvd else None
            return
        self.closed_code = self.ws.close_code

    async def send(self, data) -> None:
        await self.ws.send(data)

    def binary(self, tag: int) -> list[tuple[int, bytes]]:
        """(seq, envelope) of the 0x01/0x03 messages received; (0, envelope) for 0x02."""
        out = []
        for m in self.received:
            if isinstance(m, bytes) and m and m[0] == tag:
                if tag == T_PRESENCE:
                    out.append((0, m[1:]))
                else:
                    out.append((int.from_bytes(m[1:9], "big"), m[9:]))
        return out

    def texts(self) -> list[dict]:
        out = []
        for m in self.received:
            if isinstance(m, str) and m not in ("ping", "pong"):
                out.append(json.loads(m))
        return out

    async def close(self) -> None:
        await self.ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
