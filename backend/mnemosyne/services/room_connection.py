"""The owner side's connection to one room on a publish host (or the rooms sidecar's loopback
host): the dial, the join, the pump, the acknowledgements and the reconnect loop.

guest_notes.Bridge and live_publications.PubBridge both keep a room open for as long as they
run. Their loops were the same loop with different edges (C10 of
docs/plans/2026-11-09-live-publications-m4.md). This module owns what they share, the wire as
the publish host's rooms.rs speaks it:

- the `websockets` dial (`max_size`, no compression, `proxy=None` on loopback), the `join`
  message and the wait for `welcome`;
- `ping` / `pong`, `ack` (pops `unacked`, then `advance(seq)`), `count`, `error` (kept as
  `last_error` for the close that follows), the binary tag dispatch with `seq` in bytes 1-9;
- the pump: what was not acknowledged on the last connection first, then the queue;
- the mapping of closes and failures to `(code, status)`, `CLOSE_LAGGED` rejoining at once,
  and the backoff table walk.

What differs stays in the bridges, as `RoomHooks`: what a `welcome.ended` or a close code means,
what is queued at join and cleared at disconnect, where `seq` is kept, how a frame is made, the
room lookup before a dial. The hooks run at the same points of the loop as the code they were
cut from.

No sealing, no document, no publication knowledge here.
"""

from __future__ import annotations

import asyncio
import enum
import ipaddress
import json
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

T_NOTES, T_PRESENCE, T_SNAPSHOT = 0x01, 0x02, 0x03  # (the pub bridge calls T_NOTES T_FRAME)
CLOSE_BAD_TOKEN, CLOSE_ENDED, CLOSE_DELETED, CLOSE_TOO_LARGE = 4401, 4403, 4410, 4413
CLOSE_FULL = 4429  # the room is at its connection cap: try again later (never "gone")
CLOSE_LAGGED = 4409  # the relay dropped us for falling behind: rejoin at once with `since`


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class WelcomeAction(enum.Enum):
    CONTINUE = "continue"  # joined: read and pump
    STOP = "stop"  # the loop returns (the hook has done what the end of the room needs)
    LEAVE_ROOM = "leave"  # not joined; carry on as if the room closed with CLOSE_ENDED


class ReadAction(enum.Enum):
    STOP = "stop"  # leave the connection and end the loop
    LEAVE_ROOM = "leave"  # leave the connection; carry on as if the room closed with CLOSE_ENDED


class CloseAction(enum.Enum):
    STOP = "stop"
    RETRY_NOW = "now"  # no sleep, no attempt count
    RETRY_BACKOFF = "backoff"  # sleep the table's next delay, then count the attempt


@dataclass(frozen=True)
class Decision:
    """What `on_closed` wants next. `attempt`, when given, replaces the loop's attempt counter
    first (a join resets it to 0; the pub bridge sets 1 for its one immediate retry)."""

    action: CloseAction
    attempt: int | None = None


STOP = Decision(CloseAction.STOP)
RETRY_NOW = Decision(CloseAction.RETRY_NOW)
RETRY_BACKOFF = Decision(CloseAction.RETRY_BACKOFF)


class RoomHooks:
    """The edges. Defaults are for a bridge with nothing special to do at that point."""

    # Exceptions besides the network ones that mean "cannot reach the room" (guest: its own
    # error from working out the host's address).
    unreachable_errors: tuple[type[BaseException], ...] = ()

    async def before_connect(self, attempt: int) -> int | None:
        """Before each dial. None: dial. An int: do not dial now, the hook has waited, carry on
        the loop with that attempt counter (the pub bridge's room lookup)."""
        return None

    def url(self) -> str:
        """The room's WebSocket address, worked out again for each dial (the rooms sidecar's
        port changes when it restarts). May raise what `unreachable_errors` lists."""
        raise NotImplementedError

    def join_message(self) -> dict:
        raise NotImplementedError

    async def on_welcome(self, welcome: dict) -> WelcomeAction:
        return WelcomeAction.CONTINUE

    def on_connected(self) -> None:
        """Joined, `connected` set, the pump not yet started."""

    def resend(self, unacked: list) -> list[bytes]:
        """Messages to send first on a new connection: what the last one left unacknowledged."""
        return []

    def prepare(self, item: Any) -> bytes:
        """The message for a queued item; the bridge appends to `unacked` here what the room
        will acknowledge. Runs just before the send."""
        raise NotImplementedError

    def on_ack(self, done: Any) -> None:
        """The room acknowledged `done`, the oldest unacknowledged item (when there was one)."""

    def advance(self, seq: Any) -> None:
        """The room's log reached `seq` for us: persist it."""
        raise NotImplementedError

    def on_count(self, present: int) -> None:
        """The room's `count` message."""

    async def on_text(self, msg: dict) -> ReadAction | None:
        """Every JSON message, after the loop handled ack, count and error. `media`, `ended`
        and anything else the bridge cares about lives here."""
        return None

    async def on_presence(self, payload: bytes) -> None:
        """A 0x02 message (no `seq`)."""

    async def on_frame(self, seq: int, envelope: bytes) -> None:
        """A 0x01 message with its `seq`. The hook applies it and calls `advance(seq)`."""
        raise NotImplementedError

    def on_unreachable(self, error: BaseException) -> None:
        """Could not reach the room (network error, bad handshake, no welcome)."""

    def on_refused(self, status: int) -> None:
        """The host answered the upgrade with this HTTP status."""

    def on_disconnected(self) -> None:
        """Always, after a connection attempt ended any way (`ws` and `connected` are reset)."""

    async def on_closed(
        self,
        code: int | None,
        status: int | None,
        last_error: str,
        attempt: int,
        unacked: deque,
    ) -> Decision:
        """The attempt ended without a STOP. `code`: the close code, if the room closed; `status`:
        the HTTP status, if the upgrade was refused; `last_error`: the host's last `error`
        message on that connection ("log_full"). May edit `unacked`."""
        return RETRY_BACKOFF

    def stop_requested(self) -> bool:
        """Checked after each attempt ended (the pub bridge, when it is being retired)."""
        return False


class RoomConnection:
    def __init__(
        self,
        hooks: RoomHooks,
        *,
        outq: asyncio.Queue,
        max_message: int,
        backoff: Sequence[float] | Callable[[], Sequence[float]],
        join_timeout: float,
        label: str,
    ) -> None:
        self.hooks = hooks
        self.outq = outq
        self.max_message = max_message
        self._backoff = backoff
        self.join_timeout = join_timeout
        self.label = label  # for the log: "Guest notes of <session>"
        self.ws: Any = None
        self.connected = asyncio.Event()
        self.unacked: deque = deque()
        self.joins = 0  # successful joins (tests)
        self.last_error = ""  # the host's last `error` code on this connection

    def _delay(self, attempt: int) -> float:
        table = self._backoff() if callable(self._backoff) else self._backoff
        return table[min(attempt, len(table) - 1)]

    async def run(self) -> None:
        """Connect, read, reconnect, until a hook says stop."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import (
            ConnectionClosed,
            InvalidHandshake,
            InvalidStatus,
            InvalidURI,
        )

        hooks = self.hooks
        attempt = 0
        while True:
            held = await hooks.before_connect(attempt)
            if held is not None:
                attempt = held
                continue
            code: int | None = None
            status: int | None = None  # refused before the upgrade
            self.last_error = ""
            stop = False
            try:
                # Again each time: the rooms sidecar's port changes when it restarts.
                url = hooks.url()
                host = urlsplit(url).hostname or ""
                async with connect(
                    url,
                    max_size=self.max_message,
                    compression=None,
                    open_timeout=self.join_timeout,
                    proxy=None if is_loopback(host) else True,
                ) as ws:
                    self.ws = ws
                    await ws.send(json.dumps(hooks.join_message()))
                    welcome = json.loads(await asyncio.wait_for(ws.recv(), self.join_timeout))
                    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                        raise ValueError(f"expected welcome, got {str(welcome)[:80]!r}")
                    verdict = await hooks.on_welcome(welcome)
                    if verdict is WelcomeAction.STOP:
                        stop = True
                    elif verdict is WelcomeAction.LEAVE_ROOM:
                        code = CLOSE_ENDED
                    else:
                        attempt = 0
                        self.joins += 1
                        self.connected.set()
                        hooks.on_connected()
                        pump = asyncio.create_task(self._pump(ws))
                        try:
                            read = await self._read(ws)
                        finally:
                            self.connected.clear()
                            pump.cancel()
                            await asyncio.gather(pump, return_exceptions=True)
                        if read is ReadAction.STOP:
                            stop = True
                        elif read is ReadAction.LEAVE_ROOM:
                            code = CLOSE_ENDED
                        else:
                            code = ws.close_code
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd is not None else None
            except InvalidStatus as e:
                status = e.response.status_code
                hooks.on_refused(status)
            except (
                OSError,
                InvalidHandshake,
                InvalidURI,
                TimeoutError,
                ValueError,
                *hooks.unreachable_errors,
            ) as e:
                hooks.on_unreachable(e)
            finally:
                self.ws = None
                self.connected.clear()
                hooks.on_disconnected()
            if stop or hooks.stop_requested():
                return
            decision = await hooks.on_closed(code, status, self.last_error, attempt, self.unacked)
            if decision.attempt is not None:
                attempt = decision.attempt
            if decision.action is CloseAction.STOP:
                return
            if decision.action is CloseAction.RETRY_NOW:
                continue
            await asyncio.sleep(self._delay(attempt))
            attempt += 1

    async def _pump(self, ws) -> None:
        """Sends: what was not acknowledged on the last connection first, then the queue."""
        for data in self.hooks.resend(list(self.unacked)):
            await ws.send(data)
        while True:
            item = await self.outq.get()
            await ws.send(self.hooks.prepare(item))

    async def _read(self, ws) -> ReadAction | None:
        """Until the connection closes or a hook says to leave."""
        async for msg in ws:
            if isinstance(msg, str):
                action = await self._on_text(ws, msg)
                if action is not None:
                    return action
            else:
                await self._on_binary(bytes(msg))
        return None

    async def _on_text(self, ws, text: str) -> ReadAction | None:
        if text == "ping":
            await ws.send("pong")
            return None
        if text == "pong":
            return None
        try:
            msg = json.loads(text)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        kind = msg.get("type")
        if kind == "ack":
            if self.unacked:
                self.hooks.on_ack(self.unacked.popleft())
            self.hooks.advance(msg.get("seq"))
        elif kind == "count":
            self.hooks.on_count(int(msg.get("present") or 0))
        elif kind == "error":
            # frame_too_large / log_full come before a 4413 close; bad_frame keeps the socket
            # (our frames are never empty or unknown, so that would be a host disagreement).
            self.last_error = str(msg.get("code") or "")
            logger.warning("%s: the host says %s", self.label, self.last_error)
        return await self.hooks.on_text(msg)

    async def _on_binary(self, data: bytes) -> None:
        if not data:
            return
        tag = data[0]
        if tag == T_PRESENCE:
            await self.hooks.on_presence(data[1:])
            return
        if tag != T_NOTES or len(data) < 9:
            return  # the snapshot (0x03) is ours; the rest is not used here
        await self.hooks.on_frame(int.from_bytes(data[1:9], "big"), data[9:])
