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

Phase 10 (docs/plans/2026-10-25-phase-10-tasks.md, C7-C9, C11, C17): where the room is depends
on `guest_transport` (`plan`). "iroh": the room is on this computer, on the publish host that the
rooms sidecar (`mnemosyne-link rooms`, services/link.py RoomsLink) embeds on a loopback port, and
guests reach it over iroh through one relay; the link is format 2 (`link`). "host": as in phase 9.
With an iroh room and a configured publish host, the same room (id, key, room token) is mirrored
there: a second Bridge on the same meeting (`Link`, one per room), so the host keeps a copy when
this computer is off. A guest frame one bridge accepts reaches the other through the notes room
and goes out sealed with gid "", which no bridge accepts back.

A "tunnel" room (task 20, C9/C10) is an iroh room whose loopback host is also exposed to the
internet by one tunnel (services/tunnel.py: Tailscale Funnel, else a Cloudflare quick tunnel) shared
by all tunnel rooms; its link names the tunnel's origin, which serves the guest page and the host's
WebSocket, and still carries the iroh endpoint when the room could be served over iroh too. The
tunnel is closed with the last tunnel room and reopened at startup; a quick tunnel's address
changes with every start (`guest_notes_link_changed`).

Never logged: the room key or anything derived from it.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from . import tunnel as tunnels

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..models.session import Session
    from .link import RoomsLink

logger = logging.getLogger(__name__)

# Our public guest host, once it runs ("" until then: guest notes need a configured host).
OUR_GUEST_HOST = ""
OUR_RELAY_HOST = ""  # our own iroh relay's host, once it exists (blank = none)
# Where the guest page of an iroh link is served from when no host mirrors the room
# (`guest_page_url` overrides it): GitHub Pages, from this repo.
GUEST_PAGE_ORIGIN = "https://corpetty.github.io/mnemosyne/n/"

PRIMARY, MIRROR = "primary", "mirror"  # a meeting's rooms (`Link.kind`)
# Transports whose room lives on the rooms sidecar's loopback host (served over iroh; a "tunnel"
# room is also behind the public tunnel).
LOCAL = ("iroh", "tunnel")

ROOM_DAYS = 7  # a room lives at most this long after it was made (the host clamps the same)
ENDED_HOURS = 24  # an ended room stays readable this long (at most until ROOM_DAYS)
SNAPSHOT_EVERY = 180.0  # seconds: a new snapshot at most this often, only when something changed
PRESENCE_TTL = 30.0  # seconds: a guest without a presence frame for this long has gone
OWNER_PRESENCE_EVERY = 10.0  # seconds: the owner's presence heartbeat to the room
AUTO_END_EVERY = 60.0  # seconds between checks of `auto_end_at`
TICK = 5.0  # seconds between the bridge's housekeeping passes
TUNNEL_CHECK = 5.0  # seconds between looks at whether the tunnel's process still runs
TUNNEL_RETRY = 5.0  # seconds before the one reopen of a tunnel that died
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


def configured_host(app: AppContext) -> str:
    """The publish host rooms are made (or mirrored) on: `guest_host`, else `publish_host`,
    else ours; "" when there is none. http(s) only (http for a host on this machine in tests
    and e2e)."""
    st = app.settings
    url = (st.guest_host or "").strip() or (st.publish_host or "").strip() or OUR_GUEST_HOST
    url = url.rstrip("/")
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise GuestNotesUnavailable(f"The guest notes host is not an http(s) address: {url}")
    return url


def host_url(app: AppContext) -> str:
    """`configured_host`, or GuestNotesUnavailable when there is none."""
    url = configured_host(app)
    if not url:
        raise GuestNotesUnavailable("No host for guest notes: set a publish host in Settings")
    return url


def page_origin(app: AppContext) -> str:
    """Where the guest page of an iroh link without a mirror is served from (C11)."""
    return (app.settings.guest_page_url or "").strip() or GUEST_PAGE_ORIGIN


@dataclass(frozen=True)
class Plan:
    """Where a new room goes (`plan`). `transport`: "iroh" (the rooms sidecar's loopback host,
    reached over iroh), "tunnel" (the same host, also behind a public tunnel) or "host" (a publish
    host). `host_url`: where the room is made. `mirror_host`: a publish host the room is mirrored
    on ("" none). `page_origin`: the guest page of a link that names no host
    (`GUEST_PAGE_ORIGIN` or `guest_page_url`)."""

    transport: str
    host_url: str
    mirror_host: str
    page_origin: str


async def _rooms(app: AppContext) -> RoomsLink:
    """The rooms sidecar, started if need be; GuestNotesUnavailable when it cannot start."""
    from .link import LinkError

    try:
        rooms = await app.ensure_rooms()
    except LinkError as e:
        raise GuestNotesUnavailable(f"Guest rooms cannot run on this computer: {e}") from e
    if rooms.base_url is None:  # pragma: no cover - ready means listening
        raise GuestNotesUnavailable("Guest rooms cannot run on this computer: no loopback host")
    return rooms


def _host_plan(app: AppContext, why: str = "") -> Plan:
    """The fallback to a publish host (auto): GuestNotesUnavailable when none is set."""
    host = configured_host(app)
    if not host:
        if why:
            raise GuestNotesUnavailable(f"{why}; no publish host is set for guest notes either")
        raise GuestNotesUnavailable("No host for guest notes: set a publish host in Settings")
    return Plan("host", host, "", page_origin(app))


async def plan(app: AppContext) -> Plan:
    """Where a new room goes (C9). `guest_transport`:
    - host: the configured publish host (`host_url`), GuestNotesUnavailable when none;
    - iroh: the rooms sidecar (started here), GuestNotesUnavailable with its reason when it
      cannot start;
    - tunnel: the rooms sidecar too (the tunnel is opened by `start`), GuestNotesUnavailable
      with its reason when it cannot start;
    - auto: iroh when the sidecar starts (`start` falls back to a tunnel, then to the host, when
      the room cannot be served), else the configured host (a tunnel fronts the sidecar's loopback
      host, so it cannot work without the sidecar either), else GuestNotesUnavailable.
    An iroh or tunnel room is mirrored on the configured host whenever there is one."""
    mode = app.settings.guest_transport
    origin = page_origin(app)
    if mode == "host":
        return Plan("host", host_url(app), "", origin)
    mirror = configured_host(app)
    if mode == "tunnel":
        rooms = await _rooms(app)
        assert rooms.base_url is not None
        return Plan("tunnel", rooms.base_url, mirror, origin)
    try:
        rooms = await _rooms(app)
    except GuestNotesUnavailable as e:
        if mode == "iroh":
            raise
        logger.info("Guest notes: no iroh room (%s), trying a publish host", e)
        return _host_plan(app, str(e))
    assert rooms.base_url is not None
    return Plan("iroh", rooms.base_url, mirror, origin)


def _upload_token(app: AppContext, host: str) -> str:
    """The publish host's upload token, only for the publish host itself: never handed to
    another guest host (ours, or a separate `guest_host`)."""
    token = app.settings.publish_host_token
    publish = (app.settings.publish_host or "").strip().rstrip("/")
    return token if token and publish and host == publish else ""


def ws_url(host: str, room: str) -> str:
    scheme = "wss" if host.startswith("https:") else "ws"
    return f"{scheme}:{host.split(':', 1)[1]}/live/{gf.check_room(room)}"


def relay_host(relay: str) -> str:
    """A relay URL as the link carries it: `host[:port]` exactly as stored (an IPv6 host keeps
    its brackets; a loopback http relay is the e2e dev relay)."""
    parts = urlsplit(relay.strip())
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return netloc or relay.strip().rstrip("/")


def served_from(row: dict, default_origin: str = GUEST_PAGE_ORIGIN) -> str:
    """The origin (and path) the room's guest page is served from: its host's `/n/`, a tunnel
    room's public address, the mirror host's for a mirrored iroh room, else `default_origin`
    (`page_origin`)."""
    transport = row.get("transport") or "host"
    if transport == "tunnel" and row.get("public_url"):
        return f"{row['public_url']}/n/"
    if transport not in LOCAL:
        return f"{row['host_url']}/n/"
    if row.get("mirror_host_url"):
        return f"{row['mirror_host_url']}/n/"
    return default_origin


def link(row: dict, default_origin: str = GUEST_PAGE_ORIGIN) -> str:
    """The guest link. Only the fragment carries the key.
    - a host room (format 1): `<host>/n/<room>#<key>`;
    - an iroh room (format 2, C7): `<origin>#2.<room>.<key>.<endpoint id>.<relay host[:port]>`,
      where origin is the mirror host's `/n/<room>` (the page falls back to its WebSocket) or,
      without a mirror, `default_origin` (`page_origin`: no room in the path);
    - a tunnel room: the same on the tunnel's `<public_url>/n/<room>` (which has the WebSocket
      too); format 1 on it when the room could not be served over iroh (no endpoint)."""
    key = gf.key_to_fragment(row["key"])
    transport = row.get("transport") or "host"
    if transport == "tunnel" and row.get("public_url"):
        origin = f"{row['public_url']}/n/{row['room']}"
        if not row.get("endpoint_id") or not row.get("relay"):
            return f"{origin}#{key}"
        return f"{origin}#2.{row['room']}.{key}.{row['endpoint_id']}.{relay_host(row['relay'])}"
    if transport not in LOCAL:
        return f"{row['host_url']}/n/{row['room']}#{key}"
    origin = served_from(row, default_origin)
    if row.get("mirror_host_url"):
        origin += row["room"]
    return f"{origin}#2.{row['room']}.{key}.{row['endpoint_id']}.{relay_host(row['relay'])}"


@dataclass(frozen=True)
class Link:
    """One room of a meeting's guest notes as its bridge and the owner's actions reach it: the
    primary (the row's `transport`) or the mirror on a publish host (C8). `seq_column` is the
    row's column for the last seq the bridge applied from it."""

    kind: str  # PRIMARY or MIRROR
    transport: str  # "host", or "iroh" / "tunnel": the rooms sidecar's loopback host
    stored_host: str  # the row's host_url / mirror_host_url
    token: str  # the publisher token there
    seq_column: str  # "last_seq" / "mirror_last_seq"

    def host(self, app: AppContext) -> str:
        """Where to reach it now. The rooms sidecar listens on a new port after each start, so
        an iroh room's address is the running sidecar's; GuestNotesError when none runs."""
        if self.transport not in LOCAL:
            return self.stored_host
        base = app.rooms_link.base_url
        if not base:
            raise GuestNotesError("Guest rooms on this computer are not running")
        return base

    def upload(self, app: AppContext, host: str) -> str:
        if self.transport in LOCAL:
            return app.rooms_link.upload_token
        return _upload_token(app, host)


def links(row: dict) -> list[Link]:
    """The meeting's rooms: the primary, then the mirror when there is one."""
    out = [Link(PRIMARY, row.get("transport") or "host", row["host_url"], row["token"], "last_seq")]
    if row.get("mirror_host_url"):
        out.append(
            Link(MIRROR, "host", row["mirror_host_url"], row["mirror_token"], "mirror_last_seq")
        )
    return out


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
    if st.guest_transport == "host":
        host_url(app)
    else:  # the rest is `plan`'s (it may start the rooms sidecar)
        configured_host(app)


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
    upload: str | None = None,
):
    """One request to the room host, retried once on a network error or a 5xx (not 507).
    `upload`: the X-Upload-Token to send (None: the publish host's, `_upload_token`). A host on
    this machine is asked without proxies (C17). Raises GuestNotesError when it cannot be
    reached."""
    import httpx

    hdrs = {"Authorization": f"Bearer {token}", **(headers or {})}
    if upload is None:
        upload = _upload_token(app, host)
    if upload:
        hdrs["X-Upload-Token"] = upload
    if content is not None:
        hdrs["Content-Type"] = "application/octet-stream"
    trust_env = not _is_loopback(urlsplit(host).hostname or "")
    resp = None
    for attempt in (0, 1):
        try:
            async with httpx.AsyncClient(
                timeout=60, transport=app.http_transport, trust_env=trust_env
            ) as client:
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


async def _call(
    app: AppContext,
    target: Link,
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    """`_request` to one of the meeting's rooms (where it is now, with its tokens)."""
    host = target.host(app)
    return await _request(
        app,
        method,
        host,
        path,
        target.token,
        content=content,
        headers=headers,
        upload=target.upload(app, host),
    )


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


async def post_snapshot(
    app: AppContext, session_id: str, row: dict, since: int, target: Link | None = None
) -> None:
    """Seal and post the room's snapshot (to `target`, default the primary room); the host
    drops its log up to `since`. Raises GuestNotesError."""
    target = target or links(row)[0]
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
    resp = await _call(
        app,
        target,
        "PUT",
        f"/live/{row['room']}/snapshot",
        content=env,
        headers={"X-Room-Since": str(max(0, int(since)))},
    )
    _ok(resp, "snapshot")


# ---- the registry ---------------------------------------------------------------------------


@dataclass
class _Registry:
    bridges: dict[tuple[str, str], Bridge] = field(default_factory=dict)  # (session, kind)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    # The rooms sidecar's life: serving rooms and stopping it when none is left.
    rooms_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expiry: dict[str, asyncio.Task] = field(default_factory=dict)  # ended iroh rooms
    # The one public tunnel in front of the sidecar's loopback host, shared by every tunnel room
    # (lock order: rooms_lock, then tunnel_lock).
    tunnel: Any = None
    tunnel_port: int = 0
    tunnel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tunnel_watch: asyncio.Task | None = None

    def lock(self, session_id: str) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())


def _registry(app: AppContext) -> _Registry:
    if app.guest_bridges is None:
        app.guest_bridges = _Registry()
        app.rooms_link.on_restart.append(lambda: _reserve(app))
    return app.guest_bridges


def bridge_for(app: AppContext, session_id: str, kind: str = PRIMARY) -> Bridge | None:
    b = _registry(app).bridges.get((session_id, kind))
    return b if b is not None and b.running else None


def bridges_of(app: AppContext, session_id: str) -> list[Bridge]:
    """The meeting's running bridges, the primary first."""
    return [b for k in (PRIMARY, MIRROR) if (b := bridge_for(app, session_id, k)) is not None]


def _guest_view(app: AppContext, session_id: str) -> tuple[list[str], list[str]]:
    """Who is in any of the meeting's rooms, and who of them is typing."""
    present: dict[str, None] = {}
    typing: dict[str, None] = {}
    for b in bridges_of(app, session_id):
        p, t = b._guest_view()
        present.update(dict.fromkeys(p))
        typing.update(dict.fromkeys(t))
    return list(present), list(typing)


def guests_present(app: AppContext, session_id: str) -> list[str]:
    return _guest_view(app, session_id)[0]


def _announce_guests(app: AppContext, session_id: str) -> None:
    present, typing = _guest_view(app, session_id)
    shared_notes.broadcast_json(
        app, session_id, {"type": "guests", "present": present, "typing": typing}
    )


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
    room ended (4403, `ended`) or is gone (4410), marking the row so (the primary room; the
    mirror's going only stops its own bridge). `target` is which of the meeting's rooms it
    joins (default the primary)."""

    def __init__(
        self, app: AppContext, session_id: str, row: dict, target: Link | None = None
    ) -> None:
        self.app = app
        self.session_id = session_id
        self.row = dict(row)
        self.target = target or links(row)[0]
        self.room: str = row["room"]
        self.key: bytes = row["key"]
        # every room seq <= this is applied here
        self.seq: int = int(row.get(self.target.seq_column) or 0)
        member_id = "guest-bridge" if self.target.kind == PRIMARY else "guest-bridge-mirror"
        self.member = shared_notes.Member(
            ws=None, id=member_id, name="Guests", can_edit=True, hidden=True
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
        _registry(self.app).bridges[(self.session_id, self.target.kind)] = self
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
        key = (self.session_id, self.target.kind)
        if reg.bridges.get(key) is self:
            del reg.bridges[key]
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

        attempt = 0
        while True:
            code: int | None = None
            status: int | None = None  # refused before the upgrade
            self._last_error = ""
            try:
                # Again each time: the rooms sidecar's port changes when it restarts.
                url = ws_url(self.target.host(self.app), self.room)
                host = urlsplit(url).hostname or ""
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
            except (
                OSError,
                InvalidHandshake,
                InvalidURI,
                TimeoutError,
                ValueError,
                GuestNotesError,
            ) as e:
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
            column = self.target.seq_column
            self.row[column] = seq
            self.app.repo.set_guest_room(self.session_id, **{column: seq})

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
        _announce_guests(self.app, self.session_id)  # every room of the meeting together

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
                await post_snapshot(self.app, self.session_id, self.row, since, self.target)
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
        if self.target.kind == PRIMARY and now - self._last_auto_check >= AUTO_END_EVERY:
            self._last_auto_check = now
            row = self.app.repo.guest_room(self.session_id)
            due = _parse(row["auto_end_at"]) if row else None
            if due is not None and _now() >= due and not _recording(self.app, self.session_id):
                _spawn(self.app, end(self.app, self.session_id, by=""))

    # -- the host ended or deleted it --

    async def _host_ended(self, expires: Any, ended_at: Any = None) -> None:
        if self.closing:
            return
        if self.target.kind == MIRROR:
            self._mirror_gone("ended")
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
        _spawn(self.app, _follow_primary(self.app, self.session_id, "end"))

    async def _host_deleted(self) -> None:
        if self.closing:
            return
        if self.target.kind == MIRROR:
            self._mirror_gone("deleted")
            return
        row = self.app.repo.guest_room(self.session_id)
        if row is None or row["deleted_at"]:
            return
        self.app.repo.set_guest_room(self.session_id, deleted_at=_iso(_now()))
        history.log(self.app, self.session_id, "guest_notes_stopped", by="", reason="host")
        _seal(self.app, self.session_id, SEAL_STOPPED)
        _spawn(self.app, _follow_primary(self.app, self.session_id, "delete"))

    def _mirror_gone(self, how: str) -> None:
        """The mirror host ended or deleted its copy: only this bridge stops; the primary room
        and its link go on (the page falls back to iroh)."""
        logger.warning("Guest notes of %s: the mirror host %s the room", self.session_id, how)
        history.log(
            self.app,
            self.session_id,
            "guest_notes_mirror_gone",
            by="",
            how=how,
            host=self.target.stored_host,
        )


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


def _served(row: dict) -> bool:
    """An iroh or tunnel room the rooms sidecar should serve: not stopped and not expired (an
    ended room stays readable until it expires)."""
    if (row.get("transport") or "host") not in LOCAL or row.get("deleted_at"):
        return False
    expires = _parse(row.get("expires_at", ""))
    return expires is None or expires > _now()


async def start(app: AppContext, session_id: str, by: str = "") -> dict:
    """The meeting's live room: the one there is, or a new one (an ended one is deleted from
    its host first: one room per meeting) where `plan` puts it. Raises access.Hidden,
    Forbidden, GuestNotesUnavailable, GuestNotesError."""
    reg = _registry(app)
    async with reg.lock(session_id):
        return await _start_locked(app, session_id, by)


def _start_bridges(app: AppContext, row: dict, *, initial_snapshot: bool = True) -> bool:
    """A bridge for each of the meeting's rooms that has none. True when one started."""
    started = False
    for target in links(row):
        if bridge_for(app, row["session_id"], target.kind) is None:
            Bridge(app, row["session_id"], row, target).start(initial_snapshot=initial_snapshot)
            started = True
    return started


async def _create_room(app: AppContext, target: Link, row: dict, expires: datetime) -> None:
    resp = await _call(
        app,
        target,
        "PUT",
        f"/live/{row['room']}",
        headers={
            "X-Room-Token": row["room_token"],
            "X-Room-Expires": _expires_header(expires),
        },
    )
    _ok(resp, "room")


async def _open_iroh(
    app: AppContext, row: dict, expires: datetime, *, required: bool = True
) -> None:
    """Make the room on the rooms sidecar's loopback host, exactly as on a publish host, and
    serve it over iroh: fills the row's host_url, endpoint_id and relay. Under the rooms lock.
    Raises GuestNotesUnavailable (the sidecar cannot start) or GuestNotesError (the room was
    refused; a half-made one is removed). `required` False (a tunnel room): a room that cannot be
    served over iroh stays on the loopback host with no endpoint, reached through the tunnel."""
    from .link import LinkError

    rooms = await _rooms(app)
    row["host_url"] = rooms.base_url
    target = links(row)[0]
    try:
        await _create_room(app, target, row, expires)
    except GuestNotesUnavailable:
        raise
    except GuestNotesError as e:
        raise GuestNotesError(f"Guest rooms on this computer did not take the room: {e}") from e
    try:
        served = await rooms.room_start(row["room"])
        endpoint, relay = str(served.get("endpoint_id") or ""), str(served.get("relay") or "")
        if not endpoint or not relay:
            await rooms.room_stop(row["room"])
            raise LinkError("the rooms sidecar named no endpoint or relay")
    except LinkError as e:
        if not required:
            logger.warning("Guest notes room %s: not served over iroh (%s)", row["room"], e)
            row["endpoint_id"], row["relay"] = "", ""
            return
        with contextlib.suppress(GuestNotesError):
            await _delete_on(app, target, row)
        raise GuestNotesError(f"Could not serve the guest room over iroh: {e}") from e
    row["endpoint_id"], row["relay"] = endpoint, relay


async def _open_tunnel_room(app: AppContext, row: dict, expires: datetime) -> None:
    """A tunnel room: the tunnel first (a failure costs nothing to clean up), then the room on
    the loopback host, served over iroh too when that works. Fills host_url, public_url,
    endpoint_id and relay. Under the rooms lock. Raises GuestNotesUnavailable (no tunnel, no
    sidecar) or GuestNotesError (the room was refused)."""
    rooms = await _rooms(app)
    tunnel = await _tunnel_up(app, rooms)
    row["public_url"] = tunnel.url
    await _open_iroh(app, row, expires, required=False)


async def _open_mirror(app: AppContext, row: dict, host: str, expires: datetime) -> None:
    """The same room (id, key, room token) on a publish host, with its own publisher token.
    Without it the iroh room still works: a refusal is logged, not raised."""
    token = pf.new_token()
    target = Link(MIRROR, "host", host, token, "mirror_last_seq")
    try:
        await _create_room(app, target, row, expires)
    except GuestNotesError as e:
        logger.warning("Guest notes of %s: no mirror on %s: %s", row["session_id"], host, e)
        history.log(app, row["session_id"], "guest_notes_mirror_failed", host=host, error=str(e))
        return
    row["mirror_host_url"], row["mirror_token"], row["mirror_last_seq"] = host, token, 0
    app.repo.set_guest_room(row["session_id"], mirror_host_url=host, mirror_token=token)


async def _open_local_room(
    app: AppContext, session_id: str, row: dict, p: Plan, expires: datetime
) -> None:
    """Make the new room on the rooms sidecar's loopback host as `p` says and store it. In auto,
    a room that cannot be made falls back from iroh to a tunnel, then to the configured host
    (`row["transport"]` is then "host" and the caller makes that room); the reasons are kept for
    the answer when nothing works. Otherwise the failure is raised."""
    auto = app.settings.guest_transport == "auto"
    attempts = ["iroh", "tunnel"] if auto and p.transport == "iroh" else [p.transport]
    reasons: list[str] = []
    for transport in attempts:
        row["transport"] = transport
        try:
            async with _registry(app).rooms_lock:
                if transport == "iroh":
                    await _open_iroh(app, row, expires)
                else:
                    await _open_tunnel_room(app, row, expires)
                app.repo.put_guest_room(row)
            return
        except GuestNotesError as e:
            row.update(public_url="", endpoint_id="", relay="")
            if transport == "iroh":
                # the tunnel's room is made on the same host, where the failed one was deleted
                # and cannot be made again: it is a new one
                key = gf.new_key()
                row.update(room=gf.new_room(), key=key, token=pf.new_token())
                row["room_token"] = gf.room_token(key)
            if not auto:
                await _maybe_stop_rooms(app)
                raise
            logger.warning("Guest notes of %s: no %s room (%s)", session_id, transport, e)
            reasons.append(str(e))
    try:
        fallback = _host_plan(app, "; ".join(reasons))
    except GuestNotesUnavailable:
        await _maybe_stop_rooms(app)
        raise
    row.update(host_url=fallback.host_url, transport="host")


async def _start_locked(app: AppContext, session_id: str, by: str) -> dict:
    session = app.repo.get(session_id)
    if session is None:
        raise access.Hidden("Session not found")
    await asyncio.to_thread(check_may_start, app, session)
    row = app.repo.guest_room(session_id)
    if _live(row):
        assert row is not None
        if _served(row):
            await _serve_now(app, [row])
        _start_bridges(app, row)
        return row
    if row is not None and not row["deleted_at"]:  # ended: its link goes with it
        await _remove_everywhere(app, row)
    p = await plan(app)
    try:
        await asyncio.to_thread(shared_notes.ensure_doc_blocking, app, session_id)
    except shared_notes.NotesDocError as e:
        raise GuestNotesUnavailable("The notes cannot be opened now") from e
    key = gf.new_key()
    now = _now()
    expires = now + timedelta(days=ROOM_DAYS)
    row = {
        "session_id": session_id,
        "room": gf.new_room(),
        "key": key,
        "token": pf.new_token(),
        "room_token": gf.room_token(key),
        "host_url": p.host_url,
        "created_at": _iso(now),
        "started_by": by,
        "expires_at": _iso(expires),
        "last_seq": 0,
        "transport": p.transport,
    }
    if p.transport in LOCAL:
        await _open_local_room(app, session_id, row, p, expires)
    if row["transport"] == "host":
        await _create_room(app, links(row)[0], row, expires)
        app.repo.put_guest_room(row)
        await _maybe_stop_rooms(app)
    elif row["transport"] in LOCAL and p.mirror_host:
        await _open_mirror(app, row, p.mirror_host, expires)
    row = app.repo.guest_room(session_id) or row
    history.log(
        app,
        session_id,
        "guest_notes_started",
        host=row["host_url"] if row["transport"] == "host" else row["mirror_host_url"],
        transport=row["transport"],
        by=by,
    )
    _seal(app, session_id, SEAL_STARTED)
    for target in links(row):
        bridge = Bridge(app, session_id, row, target)
        # The first snapshot before the link is handed out: an early guest sees the notes at once.
        await bridge.snapshot()
        bridge.start(initial_snapshot=False)
    return row


async def _end_on(app: AppContext, target: Link, row: dict, expires: datetime) -> None:
    resp = await _call(
        app,
        target,
        "POST",
        f"/live/{row['room']}/end",
        headers={"X-Room-Expires": _expires_header(expires)},
    )
    _ok(resp, "end", gone_ok=True)


async def end(app: AppContext, session_id: str, by: str = "") -> dict | None:
    """End the live room (and its mirror): a final snapshot, then read-only until it expires
    (24 h, at most the room's 7 days; an iroh room is served until then). None when there is
    no live room. Raises GuestNotesError when the primary room's host refuses (a mirror's
    refusal is logged)."""
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
        targets = links(row)
        bridges = [bridge_for(app, session_id, t.kind) for t in targets]
        for target, bridge in zip(targets, bridges, strict=True):
            if bridge is not None:
                bridge.closing = True
                bridge.row.update(ended_at=ended_at, expires_at=final["expires_at"])
                if not await bridge.snapshot():
                    logger.warning(
                        "Guest notes of %s: ended without a final %s snapshot",
                        session_id,
                        target.kind,
                    )
            else:
                try:
                    await post_snapshot(app, session_id, final, row[target.seq_column], target)
                except GuestNotesError as e:
                    logger.warning("Guest notes of %s: no final snapshot: %s", session_id, e)
        try:
            await _end_on(app, targets[0], row, expires)
        except GuestNotesError:
            for bridge in bridges:  # still live: guests keep typing, the owner may try again
                if bridge is not None:
                    bridge.closing = False
                    bridge.row.update(ended_at="", expires_at=row["expires_at"])
            raise
        for target in targets[1:]:
            try:
                await _end_on(app, target, row, expires)
            except GuestNotesError as e:
                logger.warning("Guest notes of %s: the mirror did not end: %s", session_id, e)
        for bridge in bridges:
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
        row = app.repo.guest_room(session_id)
        if row is not None and _served(row):
            _expire_later(app, row)
        return row


async def _delete_on(app: AppContext, target: Link, row: dict) -> None:
    resp = await _call(app, target, "DELETE", f"/live/{row['room']}")
    _ok(resp, "delete", gone_ok=True)


async def _remove_iroh(app: AppContext, row: dict) -> None:
    """An iroh room gone from this computer: deleted from the loopback host and no longer
    served (its key deleted, so the link's endpoint is dead). Best effort, never raises: a
    sidecar that cannot start serves nothing."""
    from .link import LinkError

    async with _registry(app).rooms_lock:
        try:
            rooms = await _rooms(app)
        except GuestNotesUnavailable as e:
            logger.warning("Guest notes room %s not removed here: %s", row["room"], e)
            return
        try:
            await _delete_on(app, links(row)[0], row)
        except GuestNotesError as e:
            logger.warning("Guest notes room %s stays on this computer: %s", row["room"], e)
        try:
            await rooms.room_stop(row["room"])
        except LinkError as e:
            logger.warning("Guest notes room %s is still served: %s", row["room"], e)


async def _remove_everywhere(app: AppContext, row: dict) -> None:
    """Best effort: the room off its host (or this computer) and its mirror (an ended room
    replaced by a new one, a deleted meeting)."""
    for target in links(row):
        if target.transport in LOCAL:
            await _remove_iroh(app, row)
            continue
        try:
            await _delete_on(app, target, row)
        except GuestNotesError as e:
            logger.info(
                "Guest notes of %s: the old room stays on its host: %s", row["session_id"], e
            )
    _cancel_expiry(app, row["session_id"])


async def stop(app: AppContext, session_id: str, by: str = "") -> dict | None:
    """Delete the room (and its mirror) at once, live or ended. None when there is none."""
    reg = _registry(app)
    async with reg.lock(session_id):
        return await _stop_locked(app, session_id, by)


async def _stop_locked(
    app: AppContext, session_id: str, by: str, *, idle_check: bool = True
) -> dict | None:
    row = app.repo.guest_room(session_id)
    if row is None or row["deleted_at"]:
        return None
    reg = _registry(app)
    targets = links(row)
    bridges = [b for t in targets if (b := reg.bridges.get((session_id, t.kind))) is not None]
    for bridge in bridges:
        bridge.closing = True
    try:
        # The mirror first: a host that refuses leaves everything as it was.
        for target in reversed(targets):
            if target.transport not in LOCAL:
                await _delete_on(app, target, row)
    except GuestNotesError:
        for bridge in bridges:
            bridge.closing = False
        raise
    if targets[0].transport in LOCAL:
        await _remove_iroh(app, row)
    for bridge in bridges:
        await bridge.stop()
    _cancel_expiry(app, session_id)
    app.repo.set_guest_room(session_id, deleted_at=_iso(_now()))
    history.log(app, session_id, "guest_notes_stopped", by=by)
    _seal(app, session_id, SEAL_STOPPED)
    if idle_check:
        await _maybe_stop_rooms(app)
    return app.repo.guest_room(session_id)


async def new_link(app: AppContext, session_id: str, by: str = "") -> dict:
    """A new room and key; the old link stops working."""
    reg = _registry(app)
    async with reg.lock(session_id):
        await _stop_locked(app, session_id, by, idle_check=False)
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
    reg = _registry(app)
    for kind in (PRIMARY, MIRROR):
        bridge = reg.bridges.get((session_id, kind))
        if bridge is None:
            continue
        bridge.closing = True
        try:
            await bridge.stop()
        except Exception:
            logger.warning("Guest notes bridge of %s did not stop cleanly", session_id)
    try:
        await _remove_everywhere(app, row)
        await _maybe_stop_rooms(app)
    except Exception:
        logger.warning("Could not delete guest notes room %s from %s", row["room"], row["host_url"])


async def _follow_primary(app: AppContext, session_id: str, action: str) -> None:
    """The primary room's host ended ("end") or deleted ("delete") it: the mirror does the same
    and its bridge leaves; a deleted iroh room is no longer served."""
    async with _registry(app).lock(session_id):
        row = app.repo.guest_room(session_id)
        if row is None:
            return
        bridge = bridge_for(app, session_id, MIRROR)
        if bridge is not None:
            bridge.closing = True
        for target in links(row)[1:]:
            try:
                if action == "end":
                    expires = _parse(row["expires_at"]) or _now()
                    await _end_on(app, target, row, expires)
                else:
                    await _delete_on(app, target, row)
            except GuestNotesError as e:
                logger.warning("Guest notes of %s: the mirror did not follow: %s", session_id, e)
        if bridge is not None:
            await bridge.stop()
        if action == "delete" and (row.get("transport") or "host") in LOCAL:
            await _remove_iroh(app, row)
            await _maybe_stop_rooms(app)
        elif row is not None and _served(row):
            _expire_later(app, row)


# ---- the rooms sidecar's rooms --------------------------------------------------------------


async def _serve_now(app: AppContext, rows: list[dict]) -> None:
    """Serve iroh rooms again (after a start of the sidecar), with the relay their links name.
    Best effort: a room that cannot be served is logged (its bridge keeps retrying)."""
    try:
        async with _registry(app).rooms_lock:
            await _rooms(app)
            await _serve(app, rows)
            if any(_tunnel_room(r) for r in rows):
                await _reopen_tunnel(app)
    except GuestNotesUnavailable as e:
        logger.warning("Guest notes: rooms on this computer are not served: %s", e)


async def _serve(app: AppContext, rows: list[dict]) -> None:
    """`room_start` for each row (under the rooms lock, the sidecar running). The endpoint id
    stays (the sidecar keeps each room's key); if it ever changes, the row follows."""
    from .link import LinkError

    for row in rows:
        try:
            served = await app.rooms_link.room_start(row["room"], relay=row["relay"] or None)
        except LinkError as e:
            logger.warning("Guest notes room of %s is not served: %s", row["session_id"], e)
            continue
        endpoint = str(served.get("endpoint_id") or "")
        relay = str(served.get("relay") or "")
        if endpoint and relay and (endpoint, relay) != (row["endpoint_id"], row["relay"]):
            logger.warning(
                "Guest notes room of %s: a new endpoint, the link changed", row["session_id"]
            )
            app.repo.set_guest_room(row["session_id"], endpoint_id=endpoint, relay=relay)


async def _reserve(app: AppContext) -> None:
    """The rooms sidecar is up again after a crash: it serves nothing until told (RoomsLink
    `on_restart`). Every room it should serve is started again; the bridges reconnect to its
    new port by themselves."""
    async with _registry(app).rooms_lock:
        rows = [r for r in app.repo.live_guest_rooms() if _served(r)]
        if rows and app.rooms_link.status.running:
            logger.info("Guest notes: serving %d room(s) again after a restart", len(rows))
            await _serve(app, rows)
        if any(_tunnel_room(r) for r in rows) and app.rooms_link.status.running:
            await _reopen_tunnel(app)  # the sidecar's loopback port is a new one


async def _maybe_stop_rooms(app: AppContext) -> None:
    """Close the tunnel when no tunnel room needs it any more and stop the rooms sidecar when no
    room needs it (stopped or expired)."""
    rooms = app.rooms_link
    reg = _registry(app)
    if not rooms.active and reg.tunnel is None:
        return
    async with reg.rooms_lock:
        served = [r for r in app.repo.live_guest_rooms() if _served(r)]
        if reg.tunnel is not None and not any(_tunnel_room(r) for r in served):
            logger.info("Guest notes: no tunnel room is left, closing the tunnel")
            await _close_tunnel(app)
        if rooms.active and not served:
            logger.info("Guest notes: no room is served over iroh, stopping the rooms sidecar")
            await rooms.stop()


# ---- the tunnel ----------------------------------------------------------------------------


def _tunnel_room(row: dict) -> bool:
    return (row.get("transport") or "host") == "tunnel" and _served(row)


def _alive(tunnel: Any) -> bool:
    alive = getattr(tunnel, "alive", None)  # a Funnel has no process to look at
    return True if alive is None else bool(alive())


async def _stop_tunnel(tunnel: Any) -> None:
    try:
        await tunnel.stop()
    except Exception:
        logger.warning("Guest notes: the tunnel did not stop cleanly", exc_info=True)


async def _tunnel_up(app: AppContext, rooms: RoomsLink | None = None) -> Any:
    """The tunnel in front of the rooms sidecar's loopback host: the one that runs, else a new
    one (also when the sidecar listens on a new port or the process died). Under the rooms lock.
    Raises GuestNotesUnavailable with the tunnel's reason."""
    rooms = rooms or await _rooms(app)
    port = urlsplit(rooms.base_url or "").port or 0
    reg = _registry(app)
    async with reg.tunnel_lock:
        tunnel = reg.tunnel
        if tunnel is not None and reg.tunnel_port == port and _alive(tunnel):
            return tunnel
        if tunnel is not None:
            reg.tunnel = None
            _cancel_tunnel_watch(app)
            await _stop_tunnel(tunnel)
        try:
            tunnel = await tunnels.open_tunnel(app, port)
        except (tunnels.TunnelError, OSError) as e:
            raise GuestNotesUnavailable(f"Could not open a public tunnel: {e}") from e
        reg.tunnel, reg.tunnel_port = tunnel, port
        logger.info("Guest notes: %s tunnel open", getattr(tunnel, "kind", "a"))
        reg.tunnel_watch = _spawn(app, _watch_tunnel(app, tunnel))
        return tunnel


async def _close_tunnel(app: AppContext) -> None:
    """Close the tunnel, if one is open. Never raises."""
    reg = _registry(app)
    async with reg.tunnel_lock:
        tunnel, reg.tunnel = reg.tunnel, None
        _cancel_tunnel_watch(app)
        if tunnel is not None:
            await _stop_tunnel(tunnel)


def _cancel_tunnel_watch(app: AppContext) -> None:
    reg = _registry(app)
    task, reg.tunnel_watch = reg.tunnel_watch, None
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _reopen_tunnel(app: AppContext) -> bool:
    """Under the rooms lock: the tunnel for the live tunnel rooms, opened again if it is not
    there (startup, a sidecar that listens elsewhere, a dead process); their public address and
    links follow it. A changed address (a quick tunnel's is new each time) is logged in each
    meeting's history as `guest_notes_link_changed`: the old link's host part is dead, its iroh
    part works on. No tunnel room left: the tunnel is closed. Never raises; False when it could
    not be opened."""
    try:
        rows = [r for r in app.repo.live_guest_rooms() if _tunnel_room(r)]
        if not rows:
            await _close_tunnel(app)
            return True
        try:
            tunnel = await _tunnel_up(app)
        except GuestNotesError as e:
            logger.warning("Guest notes: no tunnel: %s", e)
            for r in rows:
                history.log(app, r["session_id"], "guest_notes_tunnel_failed", error=str(e))
            return False
        for r in rows:
            if r["public_url"] != tunnel.url:
                app.repo.set_guest_room(r["session_id"], public_url=tunnel.url)
                history.log(app, r["session_id"], "guest_notes_link_changed", transport="tunnel")
        return True
    except Exception:
        logger.warning("Guest notes: the tunnel could not be reopened", exc_info=True)
        return False


async def _watch_tunnel(app: AppContext, tunnel: Any) -> None:
    """Reopen a tunnel whose process died, once after a pause. Never raises into the bridge."""
    reg = _registry(app)
    try:
        while reg.tunnel is tunnel and _alive(tunnel):
            await asyncio.sleep(TUNNEL_CHECK)
        if reg.tunnel is not tunnel:
            return
        logger.warning("Guest notes: the tunnel stopped, opening it again")
        await asyncio.sleep(TUNNEL_RETRY)
        async with reg.rooms_lock:
            if reg.tunnel is tunnel:
                await _reopen_tunnel(app)  # replaces it, or leaves none (it logged why)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("Guest notes: the tunnel watch failed", exc_info=True)


def needs_awake(app: AppContext) -> bool:
    """True while an iroh or tunnel room is live (not ended, not stopped), for services/awake.py:
    people may be typing into this computer. Reads the bridges in memory, no database."""
    reg = getattr(app, "guest_bridges", None)
    if reg is None:
        return False
    return any(
        b.running
        and b.target.kind == PRIMARY
        and b.target.transport in LOCAL
        and not b.closing
        and not b.row.get("ended_at")
        and not b.row.get("deleted_at")
        for b in list(reg.bridges.values())
    )


def _cancel_expiry(app: AppContext, session_id: str) -> None:
    task = _registry(app).expiry.pop(session_id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def _expire_later(app: AppContext, row: dict) -> None:
    """An ended iroh room is served until it expires, then no longer (its key goes)."""
    from .link import LinkError

    reg = _registry(app)
    session_id = row["session_id"]
    _cancel_expiry(app, session_id)
    expires = _parse(row["expires_at"])
    if expires is None:
        return

    async def run() -> None:
        await asyncio.sleep(max(0.0, (expires - _now()).total_seconds()))
        if reg.expiry.get(session_id) is asyncio.current_task():
            del reg.expiry[session_id]
        async with reg.rooms_lock:
            if app.rooms_link.status.running:
                try:
                    await app.rooms_link.room_stop(row["room"])
                except LinkError as e:
                    logger.info("Guest notes room of %s: %s", session_id, e)
        await _maybe_stop_rooms(app)

    reg.expiry[session_id] = _spawn(app, run())


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
    """At startup: the iroh rooms served again (`room_start` with the relay their links name:
    the endpoint ids stay), ended ones until they expire; then bridges for every live room and
    its mirror (each posts a snapshot, then joins with the last seq it applied). Returns how
    many meetings got a bridge."""
    if not await asyncio.to_thread(shared_notes.available):
        return 0
    rows = [
        r
        for r in app.repo.live_guest_rooms()
        if app.repo.session_owner(r["session_id"]) is not None
    ]
    served = [r for r in rows if _served(r)]
    if served:
        await _serve_now(app, served)
        for row in served:
            if row["ended_at"]:
                _expire_later(app, row)
    started = 0
    for row in rows:
        if row["ended_at"]:
            continue
        if (row.get("transport") or "host") in LOCAL and not _served(row):
            continue  # expired while this computer was off: nothing serves it
        if _start_bridges(app, row):
            started += 1
    return started


async def shutdown(app: AppContext) -> None:
    """The backend stops: every bridge leaves (the rooms stay live on their hosts; guests keep
    typing together and the next start catches up from the log). The rooms sidecar is the
    AppContext's to stop."""
    reg = app.guest_bridges
    if reg is None:
        return
    for bridge in list(reg.bridges.values()):
        await bridge.stop()
    for task in list(reg.tasks):
        task.cancel()
    if reg.tasks:
        await asyncio.gather(*list(reg.tasks), return_exceptions=True)
    reg.expiry.clear()
    await _close_tunnel(app)  # a Funnel outlives the process otherwise


def relay_kind(relay: str) -> str:
    """Whose relay a room's address names: "" none, n0's public ones, ours, or a custom one."""
    host = (urlsplit(relay).hostname or relay) if relay else ""
    host = host.rstrip(".")  # n0 relay URLs end in a dot (fully qualified hosts)
    if not host:
        return "none"
    if OUR_RELAY_HOST and host == OUR_RELAY_HOST:
        return "ours"
    if host.endswith(".relay.n0.iroh.link"):
        return "n0"
    return "custom"
