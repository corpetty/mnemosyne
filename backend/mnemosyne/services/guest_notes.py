"""Guest notes (phase 9 of docs/plans/2026-10-12-hashseq-collaboration.md): a link that lets the
people in a meeting type into its live notes from a browser, with no install or account.

The notes stay where they are (services/shared_notes.py, the meeting's document and relay). A
room on a publish host (`guest_host`, else `publish_host`, else ours) is the meeting point: the
host relays and logs sealed envelopes it cannot read (services/guest_format.py). The owner's
backend is the bridge between the two, one asyncio task per live room (`Bridge`):

- a hidden member of the notes room (`shared_notes.attach`) catches every frame written here
  (the owner's typing, the copilot, a contact's, the server's comment marks) and sends it to the
  room sealed with gid "" and the owner's name;
- guests' notes frames come in through `shared_notes.accept_frames(..., skip=member)` as
  `guest:<gid>` / "<name> (guest)", so they are logged, folded and relayed to the owner's
  editors, and never echoed back;
- guests' comments become ordinary comments on the notes (services/comments.py with an explicit
  actor), marked by the server (`shared_notes.mark_comment`); the threads on the notes go back
  to the room after every `comments` event;
- the meeting's title, date, owner and agenda go to the room as a META envelope, again when
  they change (`session` and `copilot_notes` events);
- a sealed snapshot of everything (`PUT /live/{room}/snapshot`) at start, every 3 minutes while
  something changed, and at the end, so a late guest loads one snapshot and the log since.

The room protocol (HTTP for the publisher, a WebSocket for everyone) is in the Phase 9 task
notes (docs/plans/2026-10-24-phase-9-tasks.md, G6) and publish-host/src/rooms.rs.

Never logged: the room key or anything derived from it.
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .. import access
from ..models.comment import Comment, CommentCreate
from . import comments, history, shared_notes
from . import guest_format as gf
from . import publication_format as pf

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session

logger = logging.getLogger(__name__)

# Our public guest host, once it runs ("" until then: guest notes need a configured host).
OUR_GUEST_HOST = ""

ROOM_DAYS = 7  # a room lives at most this long after it was made (the host clamps the same)
ENDED_HOURS = 24  # an ended room stays readable this long (at most until ROOM_DAYS)
SNAPSHOT_EVERY = 180.0  # seconds: a new snapshot at most this often, only when something changed
PRESENCE_TTL = 30.0  # seconds: a guest without a presence frame for this long has gone
OWNER_PRESENCE_EVERY = 10.0  # seconds: the owner's presence heartbeat to the room
AUTO_END_EVERY = 60.0  # seconds between checks of `auto_end_at`
TICK = 5.0  # seconds between the bridge's housekeeping passes
BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)  # reconnect delays, the last one repeated
JOIN_TIMEOUT = 15.0  # seconds to wait for `welcome`
RETRY_DELAY = 1.0  # before the one retry of an HTTP request to the host
MAX_MESSAGE = 16 * 1024 * 1024  # a 0x03 snapshot can be 8 MiB, a notes frame 2 MiB
MAX_ENVELOPE = 2 * 1024 * 1024 + 1024  # what the host takes in one frame (G6)

T_NOTES, T_PRESENCE, T_SNAPSHOT = 0x01, 0x02, 0x03
CLOSE_BAD_TOKEN, CLOSE_ENDED, CLOSE_DELETED, CLOSE_TOO_LARGE = 4401, 4403, 4410, 4413
CLOSE_FULL = 4429  # the room is at its connection cap: try again later (never "gone")
CLOSE_LAGGED = 4409  # the relay dropped us for falling behind: rejoin at once with `since`

# Why a record version is sealed (services/records.py EVENT_SEALS), as the records list shows it.
SEAL_STARTED = "guest notes started"
SEAL_ENDED = "guest notes ended"
SEAL_STOPPED = "guest notes stopped"

_GID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
MAX_NAME = 80  # characters of a guest's name
GUEST_PREFIX = "guest:"


class GuestNotesError(RuntimeError):
    """Guest notes failed in a way the person can act on (the host refused or is unreachable);
    the message says how."""


class GuestNotesUnavailable(GuestNotesError):
    """Guest notes cannot start for this meeting or on this computer (answered 409)."""


# ---- where and who -----------------------------------------------------------------------


def host_url(app: AppContext) -> str:
    """The host rooms are made on: `guest_host`, else `publish_host`, else ours. http(s) only
    (http for a host on this machine in tests and e2e)."""
    st = app.settings
    url = (st.guest_host or "").strip() or (st.publish_host or "").strip() or OUR_GUEST_HOST
    url = url.rstrip("/")
    if not url:
        raise GuestNotesUnavailable("No host for guest notes: set a publish host in Settings")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise GuestNotesUnavailable(f"The guest notes host is not an http(s) address: {url}")
    return url


def _upload_token(app: AppContext, host: str) -> str:
    """The publish host's upload token, only for the publish host itself: never handed to
    another guest host (ours, or a separate `guest_host`)."""
    token = app.settings.publish_host_token
    publish = (app.settings.publish_host or "").strip().rstrip("/")
    return token if token and publish and host == publish else ""


def ws_url(host: str, room: str) -> str:
    scheme = "wss" if host.startswith("https:") else "ws"
    return f"{scheme}:{host.split(':', 1)[1]}/live/{gf.check_room(room)}"


def link(row: dict) -> str:
    """The guest link: `<host>/n/<room>#<key>`. Only the fragment carries the key."""
    return f"{row['host_url']}/n/{row['room']}#{gf.key_to_fragment(row['key'])}"


def state(row: dict) -> str:
    """ "live", "ended" (read-only until it expires) or "stopped" (deleted)."""
    if row.get("deleted_at"):
        return "stopped"
    return "ended" if row.get("ended_at") else "live"


def owner_name(app: AppContext, session: Session) -> str:
    """What guests see as the meeting's owner."""
    if session.owner_id:
        user = app.users.get(session.owner_id)
        if user is not None and user.name.strip():
            return user.name.strip()
    name = (app.settings.local_speaker_name or "").strip()
    if name and name.casefold() not in ("me", "you"):
        return name
    from .team_host import owner_name as account_name

    return account_name()


def check_may_start(app: AppContext, session: Session) -> None:
    """Forbidden (team server rules) or GuestNotesUnavailable. Blocking the first time (the
    sidecar probe): call it in a thread."""
    st = app.settings
    if st.team_mode and not st.allow_guest_notes:
        raise access.Forbidden("Guest notes are turned off on this server; an admin can allow them")
    if not access.can_write(session.owner_id):
        raise access.Forbidden(
            "Only the meeting's owner or an admin can invite guests to its notes"
        )
    if session.shared_from:
        raise GuestNotesUnavailable(
            "Someone shared this meeting with you: only its owner can invite guests to its notes"
        )
    if not shared_notes.available():
        raise GuestNotesUnavailable(
            "Live notes are not available on this computer (the mnemosyne-link sidecar is missing)"
        )
    if shared_notes.needs_doc_from_sync(app, session.id):
        raise GuestNotesUnavailable("The notes of this meeting have not arrived from sync yet")
    host_url(app)


# ---- time ---------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _expires_header(dt: datetime) -> str:
    """`X-Room-Expires`: RFC 3339 in UTC."""
    return _iso(dt).replace("+00:00", "Z")


def _parse(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _from_host_time(value: Any) -> str:
    """An expiry the host reports (unix seconds or ISO text) as our ISO text; "" if unknown."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return _iso(datetime.fromtimestamp(value, UTC))
    if isinstance(value, str):
        dt = _parse(value)
        return _iso(dt) if dt else ""
    return ""


# ---- the host's HTTP side --------------------------------------------------------------------


def _host_message(status: int, body: dict, what: str) -> str:
    code = str(body.get("error") or body.get("detail") or "")
    if status == 401:
        return "The guest notes host wants this room's token and none was sent"
    if status == 403 and code == "upload_token_required":
        return (
            "The publish host only takes uploads with its upload token: "
            "set it in Settings (publish host token)"
        )
    if status == 403:
        return f"The guest notes host refused this room's token{': ' + code if code else ''}"
    if status == 404 and what == "room":
        return "The publish host does not offer guest notes (it may need an update)"
    if status == 404:
        return "The guest notes room is gone from the host"
    if status == 410:
        return "The guest notes room was deleted from the host"
    if status == 409 and code in ("since_ahead", "snapshot_stale"):
        return f"The guest notes host has a newer snapshot or log than this one ({code})"
    if status == 409:
        return f"The guest notes host refused the {what}: it exists already or has ended"
    if status == 413:
        return "The notes are too large for the guest notes host"
    if status == 507:
        return "The guest notes host is full"
    if status == 429:
        return "The guest notes host is busy; try again in a minute"
    return f"The guest notes host refused the {what}: {status} {code}".rstrip()


def _body(resp) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


async def _request(
    app: AppContext,
    method: str,
    host: str,
    path: str,
    token: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    """One request to the room host, retried once on a network error or a 5xx (not 507).
    Raises GuestNotesError when it cannot be reached."""
    import httpx

    hdrs = {"Authorization": f"Bearer {token}", **(headers or {})}
    if upload := _upload_token(app, host):
        hdrs["X-Upload-Token"] = upload
    if content is not None:
        hdrs["Content-Type"] = "application/octet-stream"
    resp = None
    for attempt in (0, 1):
        try:
            async with httpx.AsyncClient(timeout=60, transport=app.http_transport) as client:
                resp = await client.request(method, host + path, content=content, headers=hdrs)
        except httpx.HTTPError as e:
            if attempt:
                raise GuestNotesError(f"Could not reach the guest notes host: {e}") from e
            await asyncio.sleep(RETRY_DELAY)
            continue
        if attempt == 0 and resp.status_code >= 500 and resp.status_code != 507:
            await asyncio.sleep(RETRY_DELAY)
            continue
        return resp
    return resp  # pragma: no cover


def _ok(resp, what: str, *, gone_ok: bool = False) -> None:
    if resp.status_code < 300:
        return
    if gone_ok and resp.status_code in (404, 410):
        return
    raise GuestNotesError(_host_message(resp.status_code, _body(resp), what))


# ---- what goes into the room ------------------------------------------------------------------


def _guest_comment(c: Comment) -> dict:
    """A comment as guests see it: no user ids or mentions; `gid` names a guest's own."""
    gid = c.author_id[len(GUEST_PREFIX) :] if c.author_id.startswith(GUEST_PREFIX) else ""
    return {
        "id": c.id,
        "parent_id": c.parent_id,
        "author": c.author,
        "gid": gid,
        "text": "" if c.deleted else c.text,
        "quote": c.quote,
        "mark": str(c.anchor.get("mark", "")) if not c.parent_id else "",
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
        "resolved": c.resolved_at is not None,
        "deleted": c.deleted,
        "detached": c.detached,
        "changed": c.changed,
    }


def threads_for_guests(app: AppContext, session_id: str) -> list[dict]:
    """The threads on the meeting's notes, oldest first, as guests see them."""
    with shared_notes.unrestricted():
        listed = comments.threads(app, session_id)
    return [
        {"root": _guest_comment(t.root), "replies": [_guest_comment(r) for r in t.replies]}
        for t in listed.threads
        if t.root.target == "notes"
    ]


def meta_for_guests(app: AppContext, session_id: str, row: dict | None = None) -> dict:
    """{title, date, owner, agenda: [{text, covered}], origin, ended, ended_at}. `origin` is the
    notes document's hashseq origin, which a guest needs to open it."""
    with shared_notes.unrestricted():
        session = app.repo.get(session_id)
    if session is None:
        return {}
    created = session.created_at
    return {
        "title": session.name,
        "date": created.astimezone().isoformat(timespec="seconds"),
        "owner": owner_name(app, session),
        "agenda": [{"text": a.text, "covered": a.covered} for a in session.agenda],
        "origin": shared_notes.notes_origin(session_id),
        "ended": bool(row and row.get("ended_at")),
        "ended_at": (row or {}).get("ended_at", ""),
    }


def _snapshot_blocking(app: AppContext, session_id: str) -> tuple[str, list[str]]:
    """Fold, then (snapshot, unfolded frames) as base64url, read together."""
    shared_notes.fold_blocking(app, session_id)
    doc, ops = app.repo.notes_catchup(session_id)
    snap = doc["snapshot"] if doc else None
    return (
        pf.b64url_encode(bytes(snap)) if snap is not None else "",
        [pf.b64url_encode(f) for _, f in ops],
    )


async def post_snapshot(app: AppContext, session_id: str, row: dict, since: int) -> None:
    """Seal and post the room's snapshot; the host drops its log up to `since`. Raises
    GuestNotesError."""
    notes, frames = await asyncio.to_thread(_snapshot_blocking, app, session_id)
    obj = {
        "format": gf.FORMAT,
        "notes": notes,
        "origin": shared_notes.notes_origin(session_id),
        "frames": frames,
        "threads": await asyncio.to_thread(threads_for_guests, app, session_id),
        "meta": await asyncio.to_thread(meta_for_guests, app, session_id, row),
    }
    env = gf.seal_json(row["key"], row["room"], gf.Kind.SNAPSHOT, obj)
    resp = await _request(
        app,
        "PUT",
        row["host_url"],
        f"/live/{row['room']}/snapshot",
        row["token"],
        content=env,
        headers={"X-Room-Since": str(max(0, int(since)))},
    )
    _ok(resp, "snapshot")


# ---- the registry ---------------------------------------------------------------------------


@dataclass
class _Registry:
    bridges: dict[str, Bridge] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())


def _registry(app: AppContext) -> _Registry:
    if app.guest_bridges is None:
        app.guest_bridges = _Registry()
    return app.guest_bridges


def bridge_for(app: AppContext, session_id: str) -> Bridge | None:
    b = _registry(app).bridges.get(session_id)
    return b if b is not None and b.running else None


def guests_present(app: AppContext, session_id: str) -> list[str]:
    b = bridge_for(app, session_id)
    return b.present() if b is not None else []


def _spawn(app: AppContext, coro) -> asyncio.Task:
    """A task outside the caller's context (a request's principal must not follow it)."""
    reg = _registry(app)
    task = asyncio.get_running_loop().create_task(coro, context=contextvars.Context())
    reg.tasks.add(task)
    task.add_done_callback(reg.tasks.discard)
    return task


# ---- the bridge ---------------------------------------------------------------------------------


@dataclass
class _Guest:
    name: str
    typing: bool
    seen: float


class Bridge:
    """One live room joined to one meeting's notes. `start()` runs it as a task; `stop()` ends
    it (detaches from the notes, closes the socket). It stops itself when the host says the
    room ended (4403, `ended`) or is gone (4410), marking the row so."""

    def __init__(self, app: AppContext, session_id: str, row: dict) -> None:
        self.app = app
        self.session_id = session_id
        self.row = dict(row)
        self.room: str = row["room"]
        self.key: bytes = row["key"]
        self.seq: int = int(row.get("last_seq") or 0)  # every room seq <= this is applied here
        self.member = shared_notes.Member(
            ws=None, id="guest-bridge", name="Guests", can_edit=True, hidden=True
        )
        self.outq: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.unacked: deque[bytes] = deque()  # 0x01 envelopes sent, not acknowledged yet
        self.guests: dict[str, _Guest] = {}
        self.present_count = 0
        self.dirty = False
        self.connected = asyncio.Event()
        self.ws: Any = None
        self.task: asyncio.Task | None = None
        self.running = False
        # The owner is ending or deleting the room: the host's notice of it is no news.
        self.closing = False
        self.joins = 0  # successful joins (tests)
        self._last_error = ""  # the host's last `error` code on this connection
        self._owner = self._owner_name()
        self._last_meta: dict | None = None
        self._last_snapshot = -1e18
        self._last_owner_presence = -1e18
        self._last_auto_check = -1e18
        self._snap_lock = asyncio.Lock()
        self._sub: asyncio.Queue | None = None

    # -- life --

    def start(self, *, initial_snapshot: bool = True) -> None:
        if self.task is not None:
            return
        self.running = True
        shared_notes.attach(self.app, self.session_id, self.member)
        self._sub = self.app.bus.subscribe()
        _registry(self.app).bridges[self.session_id] = self
        self.task = _spawn(self.app, self._run(initial_snapshot))

    async def stop(self) -> None:
        task = self.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.wait([task])
        self._cleanup()

    def _cleanup(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._sub is not None:
            self.app.bus.unsubscribe(self._sub)
            self._sub = None
        shared_notes.detach(self.app, self.session_id, self.member)
        reg = _registry(self.app)
        if reg.bridges.get(self.session_id) is self:
            del reg.bridges[self.session_id]
        if self.guests:
            self.guests.clear()
            self._announce_guests()

    async def _run(self, initial_snapshot: bool) -> None:
        helpers = [asyncio.create_task(self._convert()), asyncio.create_task(self._events())]
        try:
            self.send_meta(force=True)
            if initial_snapshot:
                await self.snapshot()
            helpers.append(asyncio.create_task(self._ticker()))
            await self._connect_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("The guest notes bridge of %s failed", self.session_id, exc_info=True)
        finally:
            for t in helpers:
                t.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            if self.ws is not None:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
            self._cleanup()

    def _owner_name(self) -> str:
        with shared_notes.unrestricted():
            session = self.app.repo.get(self.session_id)
        return owner_name(self.app, session) if session is not None else ""

    # -- the socket --

    async def _connect_loop(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import (
            ConnectionClosed,
            InvalidHandshake,
            InvalidStatus,
            InvalidURI,
        )

        url = ws_url(self.row["host_url"], self.room)
        host = urlsplit(url).hostname or ""
        attempt = 0
        while True:
            code: int | None = None
            status: int | None = None  # refused before the upgrade
            self._last_error = ""
            try:
                async with connect(
                    url,
                    max_size=MAX_MESSAGE,
                    compression=None,
                    open_timeout=JOIN_TIMEOUT,
                    proxy=None if _is_loopback(host) else True,
                ) as ws:
                    self.ws = ws
                    await ws.send(
                        json.dumps(
                            {"type": "join", "token": self.row["room_token"], "since": self.seq}
                        )
                    )
                    welcome = json.loads(await asyncio.wait_for(ws.recv(), JOIN_TIMEOUT))
                    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                        raise ValueError(f"expected welcome, got {str(welcome)[:80]!r}")
                    if welcome.get("ended"):
                        await self._host_ended(welcome.get("expires"), welcome.get("ended_at"))
                        return
                    attempt = 0
                    self.joins += 1
                    self.present_count = int(welcome.get("present") or 0)
                    if int(welcome.get("snapshot_seq") or 0) > self.seq:
                        logger.warning(
                            "Guest notes of %s: the room's snapshot is newer than this computer",
                            self.session_id,
                        )
                    self.connected.set()
                    pump = asyncio.create_task(self._pump(ws))
                    try:
                        if await self._read(ws):
                            return
                    finally:
                        self.connected.clear()
                        pump.cancel()
                        await asyncio.gather(pump, return_exceptions=True)
                    code = ws.close_code
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd is not None else None
            except InvalidStatus as e:
                status = e.response.status_code
                logger.info("Guest notes of %s: the host answered %s", self.session_id, status)
            except (OSError, InvalidHandshake, InvalidURI, TimeoutError, ValueError) as e:
                logger.info("Guest notes of %s: cannot reach the room: %s", self.session_id, e)
            finally:
                self.ws = None
                self.connected.clear()
            expired = (_parse(self.row.get("expires_at", "")) or _now()) < _now()
            if code == CLOSE_DELETED or status == 410 or (status == 404 and expired):
                await self._host_deleted()
                return
            if code == CLOSE_ENDED:
                await self._host_ended(None)
                return
            if code == CLOSE_TOO_LARGE and self._last_error == "log_full":
                # The room's log is full: a snapshot prunes it (X-Room-Since), then rejoin.
                self.dirty = True
                await self.snapshot()
            elif code == CLOSE_TOO_LARGE and self.unacked:
                # Refused for its size: sent again it would be refused again, forever. Ours
                # are kept under the host's cap, so this is a host with a smaller one: drop
                # the largest frame not acknowledged.
                largest = max(self.unacked, key=len)
                self.unacked.remove(largest)
                logger.warning(
                    "Guest notes of %s: the host refused a frame as too large", self.session_id
                )
            if code == CLOSE_LAGGED:
                attempt = 0
                continue
            if code == CLOSE_FULL:
                logger.info("Guest notes of %s: the room is full, trying again", self.session_id)
            if code == CLOSE_BAD_TOKEN:
                logger.warning(
                    "Guest notes of %s: the host refused the room token", self.session_id
                )
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _pump(self, ws) -> None:
        """Sends: what was not acknowledged on the last connection first, then the queue."""
        for env in list(self.unacked):
            await ws.send(bytes([T_NOTES]) + env)
        while True:
            tag, env = await self.outq.get()
            if tag == T_NOTES:
                self.unacked.append(env)
            await ws.send(bytes([tag]) + env)

    async def _read(self, ws) -> bool:
        """Until the connection closes. True when the bridge should stop."""
        async for msg in ws:
            if isinstance(msg, str):
                if await self._on_text(ws, msg):
                    return True
            else:
                await self._on_binary(bytes(msg))
        return False

    async def _on_text(self, ws, text: str) -> bool:
        if text == "ping":
            await ws.send("pong")
            return False
        if text == "pong":
            return False
        try:
            msg = json.loads(text)
        except ValueError:
            return False
        if not isinstance(msg, dict):
            return False
        kind = msg.get("type")
        if kind == "ack":
            if self.unacked:
                self.unacked.popleft()
            self._advance(msg.get("seq"))
        elif kind == "count":
            self.present_count = int(msg.get("present") or 0)
        elif kind == "ended":
            await self._host_ended(msg.get("expires"), msg.get("ended_at"))
            return True
        elif kind == "error":
            # frame_too_large / log_full come before a 4413 close; bad_frame keeps the socket
            # (our frames are never empty or unknown, so that would be a host disagreement).
            self._last_error = str(msg.get("code") or "")
            logger.warning("Guest notes of %s: the host says %s", self.session_id, self._last_error)
        return False

    async def _on_binary(self, data: bytes) -> None:
        if not data:
            return
        tag = data[0]
        if tag == T_PRESENCE:
            self._on_presence(data[1:])
            return
        if tag == T_SNAPSHOT:
            return  # ours: posted by this bridge
        if tag != T_NOTES or len(data) < 9:
            return
        seq = int.from_bytes(data[1:9], "big")
        env = data[9:]
        try:
            await self._on_envelope(env)
        except Exception:
            logger.warning("Guest notes of %s: a frame failed", self.session_id, exc_info=True)
        self._advance(seq)

    def _advance(self, seq: Any) -> None:
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > self.seq:
            self.seq = seq
            self.row["last_seq"] = seq
            self.app.repo.set_guest_room(self.session_id, last_seq=seq)

    async def _on_envelope(self, env: bytes) -> None:
        try:
            kind, plain = gf.unseal(self.key, self.room, env)
        except gf.GuestFormatError as e:
            logger.info("Guest notes of %s: a frame did not open: %s", self.session_id, e)
            return
        if kind is gf.Kind.NOTES:
            gid, name, frame = gf.parse_notes(plain)
            if not gid:
                return  # from the owner's side: ours, in the log here already
            if not _GID.match(gid):
                return
            self._accept_notes(gid, name, frame)
        elif kind is gf.Kind.COMMENT:
            obj = json.loads(plain.decode("utf-8"))
            if isinstance(obj, dict):
                await self._on_comment(obj)
        # SNAPSHOT and META come from the owner's side only; PRESENCE travels as 0x02.

    def _accept_notes(self, gid: str, name: str, frame: bytes) -> None:
        try:
            with shared_notes.unrestricted():
                shared_notes.accept_frames(
                    self.app,
                    self.session_id,
                    [frame],
                    GUEST_PREFIX + gid,
                    _guest_name(name),
                    skip=self.member,
                    can_edit=True,
                )
        except shared_notes.FramesRefused as e:
            logger.info("Guest notes of %s: a guest frame was refused: %s", self.session_id, e)
            return
        self.dirty = True

    async def _on_comment(self, obj: dict) -> None:
        gid = str(obj.get("gid") or "")
        if not _GID.match(gid):
            return
        actor = (GUEST_PREFIX + gid, _guest_name(str(obj.get("name") or "")))
        op = obj.get("op")
        cid = str(obj.get("id") or "")
        text = str(obj.get("text") or "")
        sid = self.session_id
        try:
            with shared_notes.unrestricted():
                if op in ("create", "reply"):
                    known = self.app.repo.get_comment(cid) is not None if cid else False
                    parent = str(obj.get("parent_id") or "")
                    if op == "reply" or parent:
                        req = CommentCreate(text=text, parent_id=parent)
                    else:
                        ids = obj.get("notes_ids")
                        req = CommentCreate(
                            text=text,
                            target="notes",
                            exact=str(obj.get("quote") or ""),
                            notes_ids=[str(i) for i in ids] if isinstance(ids, list) else [],
                        )
                    c = comments.create(self.app, sid, req, comment_id=cid, actor=actor)
                    first_last = [i.strip() for i in req.notes_ids]
                    if not known and not req.parent_id and len(first_last) == 2 and all(first_last):
                        marked = await shared_notes.mark_comment(
                            self.app, sid, c.id, first_last[0], first_last[1], *actor
                        )
                        if marked:  # the ranges are known now
                            self.app.bus.publish(
                                {"type": "comments", "session_id": sid, "mentions": []}
                            )
                elif op == "edit":
                    comments.edit(self.app, sid, cid, text, actor=actor)
                elif op == "delete":
                    comments.delete(self.app, sid, cid, actor=actor)
        except (
            comments.BadComment,
            comments.CommentNotFound,
            comments.NotesNotShared,
            access.Forbidden,
            ValueError,
        ) as e:
            logger.info("Guest notes of %s: a guest comment was not taken: %s", sid, e)

    def _on_presence(self, env: bytes) -> None:
        try:
            kind, obj = gf.unseal_json(self.key, self.room, env)
        except gf.GuestFormatError:
            return
        if kind is not gf.Kind.PRESENCE or not isinstance(obj, dict):
            return
        gid = str(obj.get("gid") or "")
        if not _GID.match(gid):
            return  # "" is the owner's side: us
        before = self._guest_view()
        self.guests[gid] = _Guest(
            _guest_name(str(obj.get("name") or "")), bool(obj.get("typing")), time.monotonic()
        )
        if self._guest_view() != before:
            self._announce_guests()

    # -- guests --

    def _guest_view(self) -> tuple[list[str], list[str]]:
        ordered = list(self.guests.values())
        return (
            list(dict.fromkeys(g.name for g in ordered)),
            list(dict.fromkeys(g.name for g in ordered if g.typing)),
        )

    def present(self) -> list[str]:
        return self._guest_view()[0]

    def _announce_guests(self) -> None:
        present, typing = self._guest_view()
        shared_notes.broadcast_json(
            self.app, self.session_id, {"type": "guests", "present": present, "typing": typing}
        )

    def _expire_guests(self, now: float) -> None:
        gone = [gid for gid, g in self.guests.items() if now - g.seen > PRESENCE_TTL]
        if gone:
            for gid in gone:
                del self.guests[gid]
            self._announce_guests()

    # -- what we send --

    def _queue(self, tag: int, env: bytes) -> None:
        if len(env) + 1 > MAX_ENVELOPE:
            logger.warning("Guest notes of %s: a frame too large for the room", self.session_id)
            return
        self.outq.put_nowait((tag, env))

    def _queue_json(self, kind: gf.Kind, obj: dict) -> None:
        self._queue(T_NOTES, gf.seal_json(self.key, self.room, kind, obj))

    async def _convert(self) -> None:
        """The notes room's frames (from the hidden member) into sealed envelopes. A reload of
        the room (library sync replaced the document): attach again and post a snapshot."""
        while True:
            kind, data = await self.member.out.get()
            if kind == "bytes":
                env = gf.seal_notes(self.key, self.room, "", self._owner, bytes(data))
                self._queue(T_NOTES, env)
                self.dirty = True
            elif kind == "close":
                shared_notes.detach(self.app, self.session_id, self.member)
                shared_notes.attach(self.app, self.session_id, self.member)
                self.dirty = True
                await self.snapshot()

    def send_meta(self, *, force: bool = False) -> None:
        meta = meta_for_guests(self.app, self.session_id, self.row)
        if meta and (force or meta != self._last_meta):
            self._last_meta = meta
            self._queue_json(gf.Kind.META, meta)
            self.dirty = True

    async def send_threads(self) -> None:
        def build() -> bytes:
            threads = threads_for_guests(self.app, self.session_id)
            return gf.seal_json(
                self.key, self.room, gf.Kind.COMMENT, {"op": "threads", "threads": threads}
            )

        self._queue(T_NOTES, await asyncio.to_thread(build))
        self.dirty = True

    async def _events(self) -> None:
        """Comments and meeting changes, coalesced: at most one threads and one meta envelope
        for whatever arrived together."""
        q = self._sub
        assert q is not None
        while True:
            batch = [await q.get()]
            while not q.empty():
                batch.append(q.get_nowait())
            mine = [e for e in batch if e.get("session_id") == self.session_id]
            if any(e.get("type") == "comments" for e in mine):
                try:
                    await self.send_threads()
                except Exception:
                    logger.warning("Guest notes: threads not sent", exc_info=True)
            if any(e.get("type") in ("session", "copilot_notes") for e in mine):
                self.send_meta()

    async def snapshot(self) -> bool:
        """Post a snapshot now. False (logged) when the host did not take it."""
        async with self._snap_lock:
            since = self.seq
            self.dirty = False
            try:
                await post_snapshot(self.app, self.session_id, self.row, since)
            except GuestNotesError as e:
                self.dirty = True
                logger.warning("Guest notes of %s: snapshot not posted: %s", self.session_id, e)
                return False
            self._last_snapshot = time.monotonic()
            return True

    # -- housekeeping --

    async def _ticker(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Guest notes of %s: housekeeping failed", self.session_id, exc_info=True
                )
            await asyncio.sleep(TICK)

    async def tick(self, now: float | None = None) -> None:
        """One housekeeping pass (tests call it with `now`): guests gone quiet, the owner's
        presence, a snapshot when due, the automatic end."""
        now = time.monotonic() if now is None else now
        self._expire_guests(now)
        if self.connected.is_set() and now - self._last_owner_presence >= OWNER_PRESENCE_EVERY:
            self._last_owner_presence = now
            env = gf.seal_json(
                self.key,
                self.room,
                gf.Kind.PRESENCE,
                {"gid": "", "name": self._owner, "typing": False},
            )
            self._queue(T_PRESENCE, env)
        if self.dirty and now - self._last_snapshot >= SNAPSHOT_EVERY:
            await self.snapshot()
        if now - self._last_auto_check >= AUTO_END_EVERY:
            self._last_auto_check = now
            row = self.app.repo.guest_room(self.session_id)
            due = _parse(row["auto_end_at"]) if row else None
            if due is not None and _now() >= due and not _recording(self.app, self.session_id):
                _spawn(self.app, end(self.app, self.session_id, by=""))

    # -- the host ended or deleted it --

    async def _host_ended(self, expires: Any, ended_at: Any = None) -> None:
        if self.closing:
            return
        row = self.app.repo.guest_room(self.session_id)
        if row is None or row["deleted_at"] or row["ended_at"]:
            return
        at = _from_host_time(ended_at) or _iso(_now())
        fields = {"ended_at": at}
        if exp := _from_host_time(expires):
            fields["expires_at"] = exp
        self.app.repo.set_guest_room(self.session_id, **fields)
        history.log(self.app, self.session_id, "guest_notes_ended", by="", reason="host")
        _seal(self.app, self.session_id, SEAL_ENDED)

    async def _host_deleted(self) -> None:
        if self.closing:
            return
        row = self.app.repo.guest_room(self.session_id)
        if row is None or row["deleted_at"]:
            return
        self.app.repo.set_guest_room(self.session_id, deleted_at=_iso(_now()))
        history.log(self.app, self.session_id, "guest_notes_stopped", by="", reason="host")
        _seal(self.app, self.session_id, SEAL_STOPPED)


def _guest_name(name: str) -> str:
    clean = "".join(ch for ch in name if ch.isprintable()).strip()[:MAX_NAME].strip()
    return f"{clean or 'Guest'} (guest)"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _seal(app: AppContext, session_id: str, reason: str) -> None:
    from .records import seal_later

    seal_later(app, session_id, reason)


# ---- the owner's actions ----------------------------------------------------------------------


def _live(row: dict | None) -> bool:
    return row is not None and not row["deleted_at"] and not row["ended_at"]


async def start(app: AppContext, session_id: str, by: str = "") -> dict:
    """The meeting's live room: the one there is, or a new one (an ended one is deleted from
    its host first: one room per meeting). Raises access.Hidden, Forbidden,
    GuestNotesUnavailable, GuestNotesError."""
    reg = _registry(app)
    async with reg.lock(session_id):
        return await _start_locked(app, session_id, by)


async def _start_locked(app: AppContext, session_id: str, by: str) -> dict:
    session = app.repo.get(session_id)
    if session is None:
        raise access.Hidden("Session not found")
    await asyncio.to_thread(check_may_start, app, session)
    row = app.repo.guest_room(session_id)
    if _live(row):
        assert row is not None
        if bridge_for(app, session_id) is None:
            Bridge(app, session_id, row).start()
        return row
    if row is not None and not row["deleted_at"]:
        try:  # ended: its link goes with it
            await _delete_on_host(app, row)
        except GuestNotesError as e:
            logger.info("Guest notes of %s: the ended room stays on its host: %s", session_id, e)
    host = host_url(app)
    try:
        await asyncio.to_thread(shared_notes.ensure_doc_blocking, app, session_id)
    except shared_notes.NotesDocError as e:
        raise GuestNotesUnavailable("The notes cannot be opened now") from e
    key = gf.new_key()
    now = _now()
    row = {
        "session_id": session_id,
        "room": gf.new_room(),
        "key": key,
        "token": pf.new_token(),
        "room_token": gf.room_token(key),
        "host_url": host,
        "created_at": _iso(now),
        "started_by": by,
        "expires_at": _iso(now + timedelta(days=ROOM_DAYS)),
        "last_seq": 0,
    }
    resp = await _request(
        app,
        "PUT",
        host,
        f"/live/{row['room']}",
        row["token"],
        headers={
            "X-Room-Token": row["room_token"],
            "X-Room-Expires": _expires_header(now + timedelta(days=ROOM_DAYS)),
        },
    )
    _ok(resp, "room")
    app.repo.put_guest_room(row)
    row = app.repo.guest_room(session_id) or row
    history.log(app, session_id, "guest_notes_started", host=host, by=by)
    _seal(app, session_id, SEAL_STARTED)
    bridge = Bridge(app, session_id, row)
    # The first snapshot before the link is handed out: an early guest sees the notes at once.
    await bridge.snapshot()
    bridge.start(initial_snapshot=False)
    return row


async def end(app: AppContext, session_id: str, by: str = "") -> dict | None:
    """End the live room: a final snapshot, then read-only on the host until it expires (24 h,
    at most the room's 7 days). None when there is no live room. Raises GuestNotesError."""
    reg = _registry(app)
    async with reg.lock(session_id):
        row = app.repo.guest_room(session_id)
        if not _live(row):
            return None
        assert row is not None
        now = _now()
        ended_at = _iso(now)
        expires = now + timedelta(hours=ENDED_HOURS)
        cap = _parse(row["expires_at"]) or now + timedelta(days=ROOM_DAYS)
        expires = min(expires, cap)
        final = {**row, "ended_at": ended_at, "expires_at": _iso(expires)}
        bridge = bridge_for(app, session_id)
        if bridge is not None:
            bridge.closing = True
            bridge.row.update(ended_at=ended_at, expires_at=final["expires_at"])
            if not await bridge.snapshot():
                logger.warning("Guest notes of %s: ended without a final snapshot", session_id)
        else:
            try:
                await post_snapshot(app, session_id, final, row["last_seq"])
            except GuestNotesError as e:
                logger.warning("Guest notes of %s: no final snapshot: %s", session_id, e)
        try:
            resp = await _request(
                app,
                "POST",
                row["host_url"],
                f"/live/{row['room']}/end",
                row["token"],
                headers={"X-Room-Expires": _expires_header(expires)},
            )
            _ok(resp, "end", gone_ok=True)
        except GuestNotesError:
            if bridge is not None:  # still live: guests keep typing, the owner may try again
                bridge.closing = False
                bridge.row.update(ended_at="", expires_at=row["expires_at"])
            raise
        if bridge is not None:
            await bridge.stop()
        app.repo.set_guest_room(session_id, ended_at=ended_at, expires_at=final["expires_at"])
        history.log(
            app,
            session_id,
            "guest_notes_ended",
            by=by,
            expires_at=final["expires_at"],
            auto=not by,
        )
        _seal(app, session_id, SEAL_ENDED)
        return app.repo.guest_room(session_id)


async def _delete_on_host(app: AppContext, row: dict) -> None:
    resp = await _request(app, "DELETE", row["host_url"], f"/live/{row['room']}", row["token"])
    _ok(resp, "delete", gone_ok=True)


async def stop(app: AppContext, session_id: str, by: str = "") -> dict | None:
    """Delete the room from its host at once (live or ended). None when there is none."""
    reg = _registry(app)
    async with reg.lock(session_id):
        return await _stop_locked(app, session_id, by)


async def _stop_locked(app: AppContext, session_id: str, by: str) -> dict | None:
    row = app.repo.guest_room(session_id)
    if row is None or row["deleted_at"]:
        return None
    bridge = _registry(app).bridges.get(session_id)
    if bridge is not None:
        bridge.closing = True
    try:
        await _delete_on_host(app, row)
    except GuestNotesError:
        if bridge is not None:
            bridge.closing = False
        raise
    if bridge is not None:
        await bridge.stop()
    app.repo.set_guest_room(session_id, deleted_at=_iso(_now()))
    history.log(app, session_id, "guest_notes_stopped", by=by)
    _seal(app, session_id, SEAL_STOPPED)
    return app.repo.guest_room(session_id)


async def new_link(app: AppContext, session_id: str, by: str = "") -> dict:
    """A new room and key; the old link stops working."""
    reg = _registry(app)
    async with reg.lock(session_id):
        await _stop_locked(app, session_id, by)
        return await _start_locked(app, session_id, by)


def forget_session(app: AppContext, session_id: str):
    """Before a meeting is deleted: what to do once it is (its room, if one is not deleted
    yet, gone from its host in the background, best effort: the bridge leaves first). The
    delete route stops the room itself first; this catches every other way a meeting goes
    (sync, combining, contacts)."""
    row = app.repo.guest_room(session_id)
    if row is None or row["deleted_at"]:
        return None

    def after() -> None:
        try:
            _spawn(app, _forget(app, session_id, row))
        except RuntimeError:  # no event loop
            logger.warning("Guest notes room %s stays on %s", row["room"], row["host_url"])

    return after


async def _forget(app: AppContext, session_id: str, row: dict) -> None:
    bridge = _registry(app).bridges.get(session_id)
    if bridge is not None:
        bridge.closing = True
        try:
            await bridge.stop()
        except Exception:
            logger.warning("Guest notes bridge of %s did not stop cleanly", session_id)
    try:
        await _delete_on_host(app, row)
    except Exception:
        logger.warning("Could not delete guest notes room %s from %s", row["room"], row["host_url"])


def recording_stopped(app: AppContext, session_id: str) -> None:
    """The meeting's recording stopped: its live room ends `guest_notes_end_hours` from now."""
    row = app.repo.guest_room(session_id)
    if not _live(row):
        return
    at = _now() + timedelta(hours=app.settings.guest_notes_end_hours)
    app.repo.set_guest_room(session_id, auto_end_at=_iso(at))


def recording_started(app: AppContext, session_id: str) -> None:
    """A recording (another part of the meeting) started: the end scheduled when the last one
    stopped no longer applies; the next stop schedules a new one."""
    row = app.repo.guest_room(session_id)
    if _live(row) and row["auto_end_at"]:
        app.repo.set_guest_room(session_id, auto_end_at="")


def _recording(app: AppContext, session_id: str) -> bool:
    recording = app.active_recordings.get(session_id)
    return recording is not None and bool(getattr(recording, "is_recording", True))


async def resume(app: AppContext) -> int:
    """At startup: a bridge for every live room (each posts a snapshot, then joins with the
    last seq it applied). Returns how many started."""
    if not await asyncio.to_thread(shared_notes.available):
        return 0
    started = 0
    for row in app.repo.live_guest_rooms():
        if row["ended_at"] or bridge_for(app, row["session_id"]) is not None:
            continue
        if app.repo.session_owner(row["session_id"]) is None:
            continue
        Bridge(app, row["session_id"], row).start(initial_snapshot=True)
        started += 1
    return started


async def shutdown(app: AppContext) -> None:
    """The backend stops: every bridge leaves (the rooms stay live on their hosts; guests keep
    typing together and the next start catches up from the log)."""
    reg = app.guest_bridges
    if reg is None:
        return
    for bridge in list(reg.bridges.values()):
        await bridge.stop()
    for task in list(reg.tasks):
        task.cancel()
    if reg.tasks:
        await asyncio.gather(*list(reg.tasks), return_exceptions=True)
