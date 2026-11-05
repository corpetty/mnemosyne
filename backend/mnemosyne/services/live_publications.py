"""Live publications (docs/plans/2026-11-05-live-publications.md): keeping a published link up
to date by itself (`follow`) and taking readers' comments (`comments`).

The watcher (task 5) is here: it polls the library-sync change log (`repo.changes_since`, the
counter every travelling write already moves, C6), keeps the meetings with a followed
publication that changed, and once one has been quiet for `QUIET` seconds (or has waited
`MAX_WAIT` seconds since its first unpublished change, so people typing together for minutes
still reach readers), its last automatic version is `GAP` seconds old and no `publish` job
for it runs, asks `publishing.is_behind` (the one notion of "changed", C2) and submits the
ordinary `publish` job with the STORED selection and `auto=True`, as the meeting's owner.
A failed automatic version is retried after `BACKOFF` (1, 2, 5, 10 minutes) and shows in
`status()` as the reason. Only the origin machine publishes automatically (a meeting
recorded elsewhere, or shared with us, never).

The bridge (task 6) is the second half: one `PubBridge` per live hosted publication of this
machine (`follow` or `comments` on, the meeting recorded here), in a guest-notes room on the
publication's own host (publish-host/src/rooms.rs, created with the publication's publisher
token). The host keeps which room goes with the publication (`X-Pub-Room`, C3): at start the
bridge reads it from `GET /pub/<id>`, moves that room's expiry (`PUT /live/<room>`), or, when
there is none or it is gone, points the publication at a fresh id (`PUT /pub/<id>/room`) and
creates that room. It posts the room's snapshot (a signed THREADS), joins with the last seq it
applied, sends META, and then: a VERSION after every publish (`version_published`), META again
every 12 hours (the room's idle rule) and after every snapshot (a joiner past the snapshot must
still hear it), the room's expiry moved every 24 hours (never past the publication's), a fresh
room when the host says this one is gone (4410). Readers' frames (COMMENT) go to
`_on_reader_frame` (task 11 opens them). Every outgoing frame is a `VersionFrame`,
`ThreadsFrame` or `MetaFrame` signed with the row's signing key (pub_live_format.sign_frame).
A host without the pointer route (or without rooms) makes the publication "unsupported": no
room, readers poll. A manual Update on the other computer of a library-sync pair (C12) sends
its VERSION through a one-shot connection to the room the host names, and makes none.

Never logged or shown: the link key, the owner keys, whole publication rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from .. import access
from . import pub_live_format as plf

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..jobs import Job, JobContext
    from ..models.publication import PublicationSelection
    from ..models.session import Session

logger = logging.getLogger(__name__)

WATCH_INTERVAL = 5.0  # seconds between looks at the change counter (0: no loop, tests tick)
QUIET = 10.0  # a change counts once the meeting has been quiet this long
MAX_WAIT = 60.0  # ... or once its first unpublished change is this old, quiet or not
GAP = 30.0  # at most one automatic version per meeting in this many seconds
BACKOFF = (60.0, 120.0, 300.0, 600.0)  # after the 1st, 2nd, 3rd, 4th and later failures
PAGE = 200  # changes read from the log at a time

FAILED_PREFIX = "The last automatic update failed: "


def status(app: AppContext, row: dict) -> tuple[str, str, int]:
    """(state, reason, readers) of a live publication's channel, for `Publication.live`,
    `live_reason` and `readers`. Only called for a row with `follow` or `comments` on.

    - "live": this machine's bridge is in the room; `readers` = the room's count without it.
    - "unsupported": the host has no room pointer (or no rooms): readers poll.
    - "offline": no bridge here (another computer bridges it, or none runs) or it cannot reach
      the host; the reason is the bridge's last error, if any.

    The watcher's last failure for the meeting, when there is one, is the reason in every
    state (it is what the owner must act on)."""
    watcher = getattr(app, "live_watcher", None)
    reason = watcher.reason(row["session_id"]) if watcher is not None else ""
    bridges: Bridges | None = getattr(app, "live_bridges", None)
    bridge = bridges.get(row["id"]) if bridges is not None else None
    if bridge is None:
        return ("offline", reason, 0)
    if bridge.state == "unsupported":
        return ("unsupported", reason or UNSUPPORTED, 0)
    if bridge.connected.is_set():
        return ("live", reason, bridge.readers)
    return ("offline", reason or bridge.reason, 0)


def _follows(row: dict) -> bool:
    return not row.get("unpublished_at") and bool((row.get("selection") or {}).get("follow"))


@dataclass
class Failure:
    count: int  # automatic versions in a row that failed
    reason: str  # the last one's error
    retry_at: float  # clock time before which the watcher leaves the meeting alone


_NOBODY = object()  # no one to publish as (a team server whose owner is gone or disabled)


class Watcher:
    """Republishes followed publications when their meeting changes. `tick()` is one look and
    is what tests drive (with `clock` swapped and `interval` 0); `start()` runs it every
    `interval` seconds."""

    def __init__(self, ctx: AppContext, clock: Callable[[], float] = time.monotonic):
        self.ctx = ctx
        self.clock = clock
        self.interval = WATCH_INTERVAL
        self.quiet = QUIET
        self.max_wait = MAX_WAIT
        self.gap = GAP
        self.backoff = BACKOFF
        self._seen = 0  # the change counter last looked at
        self._dirty: dict[str, float] = {}  # session id -> clock time of its last change
        self._first: dict[str, float] = {}  # session id -> its first change not yet published
        self._last_auto: dict[str, float] = {}  # session id -> clock time of the last submit
        self._failures: dict[str, Failure] = {}
        self._task: asyncio.Task | None = None

    # ---- lifetime ---------------------------------------------------------------------------

    def start(self) -> None:
        """At startup (after unlocking): every followed publication is looked at once (one that
        fell behind while this backend was down, or whose last automatic version failed, is
        caught up), then the change log is watched."""
        self._seen = self.ctx.repo.current_sync_seq()
        now = self.clock()
        for row in self.ctx.repo.publications():
            if _follows(row):
                self._dirty.setdefault(row["session_id"], now)
                self._first.setdefault(row["session_id"], now)
        if self.interval and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.interval)

    # ---- what the UI shows ------------------------------------------------------------------

    def reason(self, session_id: str) -> str:
        """Why the meeting's link is not up to date, for `live_reason`: "" unless its last
        automatic version failed."""
        failure = self._failures.get(session_id)
        return FAILED_PREFIX + failure.reason if failure else ""

    def failure(self, session_id: str) -> Failure | None:
        return self._failures.get(session_id)

    # ---- one look ---------------------------------------------------------------------------

    async def tick(self) -> list[Job]:
        """Look once; returns the publish jobs submitted. Never raises."""
        try:
            if self.ctx.locked:
                return []
            return await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again
            logger.warning("Live publications: the watcher's pass failed", exc_info=True)
            return []

    async def _tick(self) -> list[Job]:
        from . import publishing

        now = self.clock()
        self._read_changes(now)
        for sid in [s for s, t in self._last_auto.items() if now - t >= self.gap]:
            del self._last_auto[sid]  # only needed while the gap runs
        submitted: list[Job] = []
        for sid, changed in list(self._dirty.items()):
            if now - changed < self.quiet and now - self._first.get(sid, changed) < self.max_wait:
                continue
            last = self._last_auto.get(sid)
            if last is not None and now - last < self.gap:
                continue
            failure = self._failures.get(sid)
            if failure is not None and now < failure.retry_at:
                continue
            if self._publishing(sid):
                continue  # looked at again once that job is over
            target = self._target(sid)
            if target is None:
                self._forget(sid)
                continue
            row, session, sel, who = target
            behind = await asyncio.to_thread(publishing.is_behind, self.ctx, row, session)
            self._dirty.pop(sid, None)
            self._first.pop(sid, None)
            if not behind:
                # Nothing a reader would see changed, or someone published it by hand.
                self._failures.pop(sid, None)
                continue
            submitted.append(self._submit(sid, sel, who))
        return submitted

    def _read_changes(self, now: float) -> None:
        """Meetings changed since the last look that have a followed publication become dirty
        (their quiet time starts again). `changes_since` leaves meetings shared with us out."""
        repo = self.ctx.repo
        seq = repo.current_sync_seq()
        if seq == self._seen:
            return
        after = self._seen
        while True:
            changes, after, more = repo.changes_since(after, PAGE)
            for sid, _seq, _vv in changes:
                row = repo.live_publication(sid)
                if row is not None and _follows(row):
                    self._dirty[sid] = now
                    self._first.setdefault(sid, now)
            if not more:
                break
        self._seen = max(seq, after)

    def _publishing(self, session_id: str) -> bool:
        return any(
            j.kind == "publish" for j in self.ctx.jobs.list(session_id=session_id, active_only=True)
        )

    def _forget(self, session_id: str) -> None:
        self._dirty.pop(session_id, None)
        self._first.pop(session_id, None)
        self._failures.pop(session_id, None)
        self._last_auto.pop(session_id, None)

    def _target(self, session_id: str) -> tuple[dict, Session, PublicationSelection, Any] | None:
        """(row, meeting, stored selection, principal) when this machine should keep the
        meeting's publication up to date, else None: no followed live publication, expired,
        a meeting recorded on another computer or shared with us, files served elsewhere,
        publishing off, a selection `check_live` refuses, or nobody to publish as."""
        from ..models.publication import PublicationSelection
        from .origin import is_foreign
        from .publishing import PublishError, _dt, _now, allowed, check_live, served_by

        app = self.ctx
        row = app.repo.live_publication(session_id)
        if row is None or not _follows(row):
            return None
        if row.get("expires_at") and _dt(row["expires_at"]) <= _now():
            return None  # the sweep buries it; an automatic version would make a new link
        session = app.repo.get(session_id)
        if session is None or is_foreign(session):
            return None
        if served_by(app, row) is not None or not allowed(app):
            return None
        try:
            sel = PublicationSelection.model_validate(row["selection"])
            check_live(session, sel)
        except (PublishError, ValueError):
            return None
        who = self._owner(session)
        if who is _NOBODY:
            return None
        return row, session, sel, who

    def _owner(self, session: Session) -> Any:
        """The principal the job runs as: the meeting's owner as a user of this backend (so a
        team server's rules, `check_may_publish`, hold), unrestricted on a desktop whose
        meetings have no such owner (as its own requests are), `_NOBODY` on a team server
        whose owner is gone or disabled."""
        user = self.ctx.users.get(session.owner_id) if session.owner_id else None
        if user is not None and not user.disabled:
            return access.Principal(user.id, user.name, user.role)
        return _NOBODY if self.ctx.settings.team_mode else None

    def _submit(self, session_id: str, sel: PublicationSelection, who: Any) -> Job:
        from . import publishing

        app = self.ctx

        async def run(job: JobContext) -> dict:
            token = access.current.set(who)
            try:
                job.update("Keeping the published link up to date", 0.0)
                result = await publishing.publish(app, session_id, sel, job, auto=True)
            except Exception as e:
                self._failed(session_id, e)
                raise
            finally:
                access.current.reset(token)
            self._failures.pop(session_id, None)
            return result

        token = access.current.set(who)  # the job is the owner's (`Job.owner_id`)
        try:
            job = app.jobs.submit("publish", run, session_id=session_id)
        finally:
            access.current.reset(token)
        self._last_auto[session_id] = self.clock()
        return job

    def _failed(self, session_id: str, error: Exception) -> None:
        """An automatic version failed (the host is unreachable, full, ...): the row stays
        behind, the reason shows, and the meeting is tried again after the backoff even if
        nothing changes meanwhile."""
        prev = self._failures.get(session_id)
        count = (prev.count if prev else 0) + 1
        now = self.clock()
        wait = self.backoff[min(count, len(self.backoff)) - 1]
        reason = str(error) or type(error).__name__
        self._failures[session_id] = Failure(count=count, reason=reason, retry_at=now + wait)
        self._dirty.setdefault(session_id, now)
        self._first.setdefault(session_id, now)
        logger.info(
            "Live publications: automatic version of %s failed (%d in a row), again in %ds: %s",
            session_id,
            count,
            int(wait),
            reason,
        )


# ---- the bridge to the host's room (task 6) -------------------------------------------------

ROOM_DAYS = 7  # a room lives at most this long (the host clamps the same, `room_max_days`)
EXTEND_EVERY = 24 * 3600.0  # seconds between moves of the room's expiry
META_EVERY = 12 * 3600.0  # seconds: META as the room's heartbeat (it idles out after 24 h)
SNAPSHOT_EVERY = 600.0  # seconds: the log pruned by a snapshot at most this often, after frames
TICK = 60.0  # seconds between the bridge's housekeeping passes
ROOM_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)  # reconnect delays, the last one repeated
RETRY_LATER = 6 * 3600.0  # seconds before a host without rooms (or the publication) is asked again
JOIN_TIMEOUT = 15.0  # seconds to wait for `welcome`
ONE_SHOT_TIMEOUT = 15.0  # seconds a one-shot VERSION may take, all in all
MAX_MESSAGE = 16 * 1024 * 1024  # a room snapshot can be 8 MiB
NO_REPLAY = 2**53 - 1  # a one-shot's `since`: none of the log, no snapshot

T_FRAME, T_PRESENCE, T_SNAPSHOT = 0x01, 0x02, 0x03
CLOSE_BAD_TOKEN, CLOSE_ENDED, CLOSE_DELETED, CLOSE_TOO_LARGE = 4401, 4403, 4410, 4413
CLOSE_LAGGED, CLOSE_FULL = 4409, 4429

UNSUPPORTED = "This host cannot push updates: readers check every minute"
NOT_ON_HOST = "The publish host no longer has this publication"


# -- the frames the bridge sends: these fields, no others (the leak test checks the wire) --


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[plf.Kind]


class VersionFrame(_Frame):
    """A new version is on the host: readers re-fetch `/pub/<id>` and take it only when its
    ETag is `sha256` and its version is higher than the one they show."""

    KIND: ClassVar[plf.Kind] = plf.Kind.VERSION
    version: int
    sha256: str
    published_at: str
    expires_at: str


class ThreadsFrame(_Frame):
    """The readers' threads (task 11 fills them; `threads_for_readers`); also the room's
    snapshot. The newest `at` wins."""

    KIND: ClassVar[plf.Kind] = plf.Kind.THREADS
    threads: list[dict[str, Any]]
    at: str


class MetaFrame(_Frame):
    """The switches as readers need them, and the room's heartbeat. The newest `at` wins."""

    KIND: ClassVar[plf.Kind] = plf.Kind.META
    comments: bool
    follow: bool
    at: str


FRAMES: dict[plf.Kind, type[_Frame]] = {
    plf.Kind.VERSION: VersionFrame,
    plf.Kind.THREADS: ThreadsFrame,
    plf.Kind.META: MetaFrame,
}


def threads_for_readers(app: AppContext, session_id: str, sel: PublicationSelection) -> list:
    """The threads readers see (task 11: readers' threads on published parts, redacted like
    the manifest). None yet."""
    return []


# -- rows, rooms, time --


def _switches(row: dict) -> tuple[bool, bool]:
    sel = row.get("selection") or {}
    return bool(sel.get("follow")), bool(sel.get("comments"))


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def wanted(row: dict | None) -> bool:
    """The publication has a live channel: on the publish host, not unpublished or expired,
    `follow` or `comments` on, its owner keys made."""
    if row is None or row.get("unpublished_at"):
        return False
    if row.get("place") != "host" or not row.get("host_url"):
        return False
    if not any(_switches(row)) or not (row.get("sign_key") and row.get("box_key")):
        return False
    expires = _parse(row.get("expires_at"))
    return expires is None or expires > datetime.now(UTC)


def _origin_here(app: AppContext, row: dict) -> bool:
    """This machine bridges the publication: publishing is on and the meeting was recorded
    here (not on the other computer of a library-sync pair, not shared with us)."""
    from .origin import is_foreign
    from .publishing import allowed
    from .shared_notes import unrestricted

    if not allowed(app):
        return False
    with unrestricted():
        session = app.repo.get(row["session_id"])
    return session is not None and not is_foreign(session) and not session.shared_from


def _valid_room(room: str) -> bool:
    from . import guest_format as gf

    try:
        gf.check_room(room)
    except gf.GuestFormatError:
        return False
    return True


def _room_file(app: AppContext, pub_id: str) -> Path:
    from .publishing import pub_dir

    return pub_dir(app, pub_id) / "room.json"


def _read_room(app: AppContext, pub_id: str) -> tuple[str, int]:
    """(room, last seq applied) this machine keeps beside the publication's files; ("", 0)."""
    try:
        data = json.loads(_room_file(app, pub_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", 0
    room, seq = data.get("room"), data.get("seq")
    if not isinstance(room, str) or not _valid_room(room):
        return "", 0
    return room, seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 0


def _write_room(app: AppContext, pub_id: str, room: str, seq: int) -> None:
    from .publishing import read_meta

    path = _room_file(app, pub_id)
    if not path.parent.is_dir() or (read_meta(app, pub_id) or {}).get("gone"):
        return  # unpublished meanwhile: no room to remember
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"room": room, "seq": seq}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("Live publication %s: could not keep its room's seq", pub_id)


def _forget_room(app: AppContext, pub_id: str) -> None:
    with contextlib.suppress(OSError):
        _room_file(app, pub_id).unlink(missing_ok=True)


def _room_expiry(row: dict) -> datetime:
    """A room never outlives the publication (C13), and lives at most ROOM_DAYS."""
    cap = datetime.now(UTC) + timedelta(days=ROOM_DAYS)
    expires = _parse(row.get("expires_at"))
    return min(cap, expires) if expires is not None else cap


def _at_text(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


async def _http(
    app: AppContext,
    method: str,
    host: str,
    path: str,
    token: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
):
    """One request to the publication's host (publishing._host: its upload token, the retry).
    Raises PublishError when it cannot be reached."""
    from .publishing import _host

    return await _host(
        app, method, host.rstrip("/") + path, token, content=content, headers=headers
    )


def _refusal(resp) -> str:
    from .publishing import _body, _host_message

    return _host_message(resp.status_code, _body(resp), "live room")


def _old_host(resp) -> bool:
    """The pointer route is missing: 404/405, or an older host that took "room" for a blob's
    sha (`PUT /pub/{id}/{sha}` checks the sha first: 400 bad_sha256)."""
    from .publishing import _body

    if resp.status_code in (404, 405):
        return True
    return resp.status_code == 400 and _body(resp).get("error") == "bad_sha256"


async def _pointed_room(app: AppContext, host: str, pub_id: str) -> str:
    """The room the host names for the publication (`X-Pub-Room`), "" when none."""
    resp = await _http(app, "GET", host, f"/pub/{pub_id}", "")
    if resp.status_code != 200:
        return ""
    room = (resp.headers.get("x-pub-room") or "").strip()
    return room if _valid_room(room) else ""


# -- the registry --


class Bridges:
    """This machine's publication bridges, by publication id (`AppContext.live_bridges`).
    `enabled` False (tests, conftest) makes the whole thing inert: no bridge, no room, no
    one-shot, no network, as the watcher's loop is off there."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.enabled = True
        self.by_id: dict[str, PubBridge] = {}
        self.tasks: set[asyncio.Task] = set()

    def get(self, pub_id: str) -> PubBridge | None:
        bridge = self.by_id.get(pub_id)
        return bridge if bridge is not None and bridge.running else None

    def spawn(self, coro) -> asyncio.Task:
        """A step outside the caller's context (a request's principal must not follow it)."""
        task = asyncio.get_running_loop().create_task(coro, context=contextvars.Context())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def settle(self) -> None:
        """Until every spawned step (retire, one-shot, restart) is done; for tests."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def ensure(self, row: dict) -> PubBridge:
        """The publication's running bridge (given the newer row), else a new one."""
        bridge = self.get(row["id"])
        if bridge is not None:
            bridge.update_row(row)
            return bridge
        bridge = PubBridge(self, row)
        bridge.start()
        return bridge

    async def resume(self) -> int:
        """At startup (after unlocking): a bridge for every live hosted publication recorded
        here. Returns how many."""
        if not self.enabled or self.ctx.locked:
            return 0
        started = 0
        for row in self.ctx.repo.publications():
            if wanted(row) and _origin_here(self.ctx, row):
                self.ensure(row)
                started += 1
        return started

    async def restart(self, old: dict, new: dict, frame: VersionFrame) -> None:
        """The publication moved to another host: the old room goes, a new bridge starts."""
        await self.retire(old)
        self.ensure(new).send_version(frame)

    async def retire(self, row: dict, *, clear_pointer: bool = False) -> None:
        """The publication's live channel ends (unpublish, expiry, the meeting deleted, both
        switches off, a move to this computer or another host): its bridge stops, then its
        room is deleted from the host (readers hear 4410); with `clear_pointer` (the
        publication stays on the host) the host stops naming it. Best effort: never raises."""
        from .publishing import PublishError

        if not self.enabled:
            return
        bridge = self.by_id.get(row["id"])
        if bridge is None and (not any(_switches(row)) or row.get("place") != "host"):
            return  # it never had a room here
        host, room = row.get("host_url") or "", ""
        if bridge is not None:
            host, room = bridge.row.get("host_url") or host, bridge.room
            bridge.closing = True
            await bridge.stop()
        room = room or _read_room(self.ctx, row["id"])[0]
        _forget_room(self.ctx, row["id"])
        if not host:
            return
        try:
            if not room:
                room = await _pointed_room(self.ctx, host, row["id"])
            if room:
                resp = await _http(self.ctx, "DELETE", host, f"/live/{room}", row["token"])
                if resp.status_code not in (200, 204, 404, 410):
                    logger.warning(
                        "Live publication %s: its room was not deleted: %s",
                        row["id"],
                        _refusal(resp),
                    )
            if clear_pointer and room:
                await _http(
                    self.ctx, "PUT", host, f"/pub/{row['id']}/room", row["token"], content=b""
                )
        except PublishError as e:
            logger.info("Live publication %s: its room stays until it expires: %s", row["id"], e)

    async def shutdown(self) -> None:
        """The backend stops: every bridge leaves; the rooms stay on their hosts."""
        for bridge in list(self.by_id.values()):
            await bridge.stop()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


def _bridges(app: AppContext) -> Bridges | None:
    bridges = getattr(app, "live_bridges", None)
    if bridges is None or not bridges.enabled:
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    return bridges


# -- what publishing calls --


def version_published(
    app: AppContext, row: dict, sha256: str, published_at: str, expires_at: str
) -> None:
    """After every publish, by hand or automatic (`row` as stored): on the origin the bridge
    (started if needed) sends VERSION {version, sha256, published_at, expires_at}; on another
    computer a one-shot connection sends it to the room the host names, if any (C12). A publish
    that turned both switches off (or moved the publication off the host) ends the room."""
    bridges = _bridges(app)
    if bridges is None:
        return
    current = bridges.get(row["id"])
    if not wanted(row):
        if current is not None:
            same_host = row.get("place") == "host" and row.get("host_url") == current.row.get(
                "host_url"
            )
            bridges.spawn(bridges.retire(current.row, clear_pointer=same_host))
        return
    frame = VersionFrame(
        version=int(row["version"]),
        sha256=sha256,
        published_at=published_at,
        expires_at=expires_at or "",
    )
    if not _origin_here(app, row):
        bridges.spawn(push_version(app, row, frame))
        return
    if current is not None and current.row.get("host_url") != row.get("host_url"):
        bridges.spawn(bridges.restart(current.row, row, frame))
        return
    bridges.ensure(row).send_version(frame)


async def retire(app: AppContext, row: dict, *, clear_pointer: bool = False) -> None:
    """Stop the publication's bridge and delete its room (Bridges.retire); unpublish calls it
    before deleting the publication, so readers hear the room go first."""
    bridges = _bridges(app)
    if bridges is not None:
        await bridges.retire(row, clear_pointer=clear_pointer)


def retire_later(app: AppContext, row: dict) -> None:
    """`retire` in the background, from code that is not async (the expiry sweep)."""
    bridges = _bridges(app)
    if bridges is not None:
        bridges.spawn(bridges.retire(row))


async def push_version(app: AppContext, row: dict, frame: VersionFrame) -> bool:
    """C12: a VERSION from a computer that does not bridge the publication (a manual Update on
    the other computer of a library-sync pair): join the room the host names (`since` past the
    log: nothing is replayed), send the signed frame, wait for its ack, leave. No room, nothing
    (readers poll). True when the room took it. Never raises."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

    from .guest_notes import _is_loopback, ws_url
    from .publishing import PublishError

    pub_id, host = row["id"], row["host_url"]
    try:
        room = await _pointed_room(app, host, pub_id)
        if not room:
            return False
        key = row["key"]
        env = plf.sign_frame(key, room, frame.KIND, frame.model_dump(mode="json"), row["sign_key"])
        url = ws_url(host, room)
        loopback = _is_loopback(urlsplit(url).hostname or "")
        async with asyncio.timeout(ONE_SHOT_TIMEOUT):
            async with connect(
                url, max_size=MAX_MESSAGE, compression=None, proxy=None if loopback else True
            ) as ws:
                hello = {"type": "join", "token": plf.room_token(key), "since": NO_REPLAY}
                await ws.send(json.dumps(hello))
                welcome = json.loads(await ws.recv())
                if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                    return False
                if welcome.get("ended"):
                    return False
                await ws.send(bytes([T_FRAME]) + env)
                async for msg in ws:
                    if not isinstance(msg, str):
                        continue
                    if msg == "ping":
                        await ws.send("pong")
                        continue
                    with contextlib.suppress(ValueError):
                        told = json.loads(msg)
                        if isinstance(told, dict) and told.get("type") == "ack":
                            return True
                        if isinstance(told, dict) and told.get("type") == "error":
                            return False
    except (
        PublishError,
        plf.PubLiveFormatError,
        ConnectionClosed,
        InvalidHandshake,
        InvalidURI,
        OSError,
        TimeoutError,
        ValueError,
    ) as e:
        logger.info("Live publication %s: the new version was not pushed: %s", pub_id, e)
    return False


# -- the bridge --


class PubBridge:
    """One live hosted publication joined to its room. `start()` runs it as a task, `stop()`
    leaves (the room stays). `tick()` is one housekeeping pass (tests call it with `now`)."""

    def __init__(self, bridges: Bridges, row: dict) -> None:
        self.bridges = bridges
        self.app = bridges.ctx
        self.row = dict(row)
        self.pub_id: str = row["id"]
        self.session_id: str = row["session_id"]
        self.key: bytes = row["key"]
        self.room_token = plf.room_token(self.key)
        self.room, self.seq = _read_room(self.app, self.pub_id)
        self.state = "offline"  # or "unsupported" (status() adds "live" while connected)
        self.reason = ""  # the last error, for `live_reason` while offline
        self.present = 0  # the room's count, this bridge included
        self.connected = asyncio.Event()
        self.outq: asyncio.Queue[_Frame] = asyncio.Queue()
        self.unacked: deque[_Frame] = deque()
        self.version: VersionFrame | None = None  # the newest, until the room acknowledges it
        self.ws: Any = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.closing = False  # retiring: the host's word that the room went is no news
        self.joins = 0  # successful joins (tests)
        self.dropped = 0  # frames from the room that are not readers' comments, or do not open
        self._refused: set[str] = set()  # rooms whose token the host refused: never again
        self._lookup = True  # look the room up on the host before the next join
        self._ended = False
        self._last_error = ""  # the host's last `error` code on this connection
        self._last_at: datetime | None = None
        self._last_meta: tuple[bool, bool] | None = None
        self._meta_at = float("-inf")
        self._extended_at = float("-inf")
        self._snapshot_at = float("-inf")
        self._since_snapshot = False  # frames sent since the last snapshot
        self._after_snapshot: _Frame | None = None  # the META sent right after it
        self._snap_lock = asyncio.Lock()

    @property
    def readers(self) -> int:
        return max(0, self.present - 1)

    # -- life --

    def start(self) -> None:
        if self.task is not None:
            return
        self.running = True
        self.bridges.by_id[self.pub_id] = self
        self.task = asyncio.get_running_loop().create_task(
            self._run(), context=contextvars.Context()
        )

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
        self.connected.clear()
        if self.bridges.by_id.get(self.pub_id) is self:
            del self.bridges.by_id[self.pub_id]

    def update_row(self, row: dict) -> None:
        """A newer row (a publish, or the tick's look): a switch turned on or off is META at
        once; a new expiry moves the room's at the next tick."""
        if row.get("expires_at") != self.row.get("expires_at"):
            self._extended_at = float("-inf")
        self.row = dict(row)
        if self._last_meta is not None and _switches(row) != self._last_meta:
            self.send_meta()

    async def _run(self) -> None:
        ticker = asyncio.create_task(self._ticker())
        try:
            await self._connect_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("The live publication bridge of %s failed", self.pub_id, exc_info=True)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            if self.ws is not None:
                with contextlib.suppress(Exception):
                    await self.ws.close()
                self.ws = None
            self._cleanup()

    # -- the room on the host --

    async def _ensure_room(self) -> str:
        """Find the publication's room on the host, or make one: "ok", "unsupported" (no
        pointer route or no rooms), "gone" (the host has no such publication) or "error"
        (`reason` says why). The pointer is set before the room is made, so an older host
        gets no room at all."""
        from .publishing import PublishError

        host, token = self.row["host_url"], self.row["token"]
        try:
            resp = await _http(self.app, "GET", host, f"/pub/{self.pub_id}", "")
            if resp.status_code in (404, 410):
                return self._unusable("offline", NOT_ON_HOST, "gone")
            if resp.status_code != 200:
                self.reason = _refusal(resp)
                return "error"
            pointed = (resp.headers.get("x-pub-room") or "").strip()
            if pointed and _valid_room(pointed) and pointed not in self._refused:
                code = await self._put_room(pointed)
                if code in (200, 201):
                    self._use(pointed, created=code == 201)
                    return "ok"
                if code == 404:
                    return self._unusable("unsupported", UNSUPPORTED, "unsupported")
                # 410 (deleted, expired or idled out: the id is refused for a while) or 403
                # (not this publication's): a fresh one.
            room = plf.new_room()
            resp = await _http(
                self.app, "PUT", host, f"/pub/{self.pub_id}/room", token, content=room.encode()
            )
            if _old_host(resp):
                return self._unusable("unsupported", UNSUPPORTED, "unsupported")
            if resp.status_code >= 300:
                self.reason = _refusal(resp)
                return "error"
            code = await self._put_room(room)
            if code == 404:  # a pointer, but no rooms on this host
                await _http(self.app, "PUT", host, f"/pub/{self.pub_id}/room", token, content=b"")
                return self._unusable("unsupported", UNSUPPORTED, "unsupported")
            if code not in (200, 201):
                return "error"
            self._use(room, created=True)
            return "ok"
        except PublishError as e:
            self.reason = str(e)
            return "error"

    async def _put_room(self, room: str) -> int:
        """`PUT /live/<room>`: creates it (201) or moves its expiry (200)."""
        from .guest_notes import _expires_header

        resp = await _http(
            self.app,
            "PUT",
            self.row["host_url"],
            f"/live/{room}",
            self.row["token"],
            headers={
                "X-Room-Token": self.room_token,
                "X-Room-Expires": _expires_header(_room_expiry(self.row)),
            },
        )
        if resp.status_code in (200, 201):
            self._extended_at = time.monotonic()
        elif resp.status_code != 404:
            self.reason = _refusal(resp)
        return resp.status_code

    def _use(self, room: str, *, created: bool) -> None:
        if room != self.room or created:
            self.seq = 0  # a new room's log starts again
        self.room = room
        self.state = "offline"
        self.reason = ""
        _write_room(self.app, self.pub_id, room, self.seq)

    def _unusable(self, state: str, reason: str, outcome: str) -> str:
        self.state = state
        self.reason = reason
        if outcome == "unsupported":
            logger.info("Live publication %s: the host has no live rooms", self.pub_id)
        return outcome

    def _lose_room(self) -> None:
        """The host says the room is gone (or refused us): look it up again, which makes a
        fresh one."""
        self._lookup = True
        self.room, self.seq = "", 0

    # -- the socket --

    async def _connect_loop(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import (
            ConnectionClosed,
            InvalidHandshake,
            InvalidStatus,
            InvalidURI,
        )

        from .guest_notes import _is_loopback, ws_url

        attempt = 0
        while True:
            if self._lookup or not self.room:
                outcome = await self._ensure_room()
                if outcome in ("unsupported", "gone"):
                    await asyncio.sleep(RETRY_LATER)
                    continue
                if outcome != "ok":
                    await asyncio.sleep(ROOM_BACKOFF[min(attempt, len(ROOM_BACKOFF) - 1)])
                    attempt += 1
                    continue
                self._lookup = False
                await self.snapshot()  # a failure is logged; the room still works without
            code: int | None = None
            status: int | None = None
            self._last_error = ""
            self._ended = False
            try:
                url = ws_url(self.row["host_url"], self.room)
                loopback = _is_loopback(urlsplit(url).hostname or "")
                async with connect(
                    url,
                    max_size=MAX_MESSAGE,
                    compression=None,
                    open_timeout=JOIN_TIMEOUT,
                    proxy=None if loopback else True,
                ) as ws:
                    self.ws = ws
                    hello = {"type": "join", "token": self.room_token, "since": self.seq}
                    await ws.send(json.dumps(hello))
                    welcome = json.loads(await asyncio.wait_for(ws.recv(), JOIN_TIMEOUT))
                    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                        raise ValueError("the room did not welcome the bridge")
                    if welcome.get("ended"):
                        code = CLOSE_ENDED  # ours are never ended: someone else's, or broken
                    else:
                        attempt = 0
                        self.joins += 1
                        self.present = int(welcome.get("present") or 0)
                        self.reason = ""
                        self.connected.set()
                        self._joined()
                        pump = asyncio.create_task(self._pump(ws))
                        try:
                            await self._read(ws)
                        finally:
                            self.connected.clear()
                            pump.cancel()
                            await asyncio.gather(pump, return_exceptions=True)
                        code = CLOSE_ENDED if self._ended else ws.close_code
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd is not None else None
            except InvalidStatus as e:
                status = e.response.status_code
                self.reason = f"The publish host refused the live room ({status})"
            except (OSError, InvalidHandshake, InvalidURI, TimeoutError, ValueError) as e:
                self.reason = "Could not reach the publish host's live room"
                logger.info("Live publication %s: cannot reach its room: %s", self.pub_id, e)
            finally:
                self.ws = None
                self.connected.clear()
                self._disconnected()
            if self.closing:
                return
            if code in (CLOSE_DELETED, CLOSE_ENDED, CLOSE_BAD_TOKEN) or status in (404, 410):
                logger.info("Live publication %s: its room is gone (%s)", self.pub_id, code)
                if code == CLOSE_BAD_TOKEN:
                    self._refused.add(self.room)
                self._lose_room()
                if attempt == 0:  # at once, the first time
                    attempt = 1
                    continue
            elif code == CLOSE_TOO_LARGE and self._last_error == "log_full":
                await self.snapshot()  # prunes the log (X-Room-Since), then rejoin
            elif code == CLOSE_LAGGED:
                attempt = 0
                continue
            elif code == CLOSE_FULL:
                self.reason = "The live room on the publish host is full"
            await asyncio.sleep(ROOM_BACKOFF[min(attempt, len(ROOM_BACKOFF) - 1)])
            attempt += 1

    def _joined(self) -> None:
        """In the room: META, and the newest VERSION the room has not acknowledged."""
        self.send_meta()
        if self.version is not None:
            self.outq.put_nowait(self.version)

    def _disconnected(self) -> None:
        """What was queued or unacknowledged is said again at the next join (META anew, the
        newest VERSION from `self.version`)."""
        self.unacked.clear()
        while not self.outq.empty():
            self.outq.get_nowait()

    async def _pump(self, ws) -> None:
        while True:
            frame = await self.outq.get()
            env = self._seal(frame)
            self.unacked.append(frame)
            if frame is not self._after_snapshot:
                self._since_snapshot = True  # (the META that follows a snapshot is no news)
            await ws.send(bytes([T_FRAME]) + env)

    def _seal(self, frame: _Frame) -> bytes:
        return plf.sign_frame(
            self.key, self.room, frame.KIND, frame.model_dump(mode="json"), self.row["sign_key"]
        )

    async def _read(self, ws) -> None:
        async for msg in ws:
            if isinstance(msg, str):
                if await self._on_text(ws, msg):
                    return
            else:
                await self._on_binary(bytes(msg))

    async def _on_text(self, ws, text: str) -> bool:
        """True when the bridge should leave the room."""
        if text == "ping":
            await ws.send("pong")
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
                done = self.unacked.popleft()
                if done is self.version:
                    self.version = None
            self._advance(msg.get("seq"))
        elif kind == "count":
            self.present = int(msg.get("present") or 0)
        elif kind == "ended":
            self._ended = True
            return True
        elif kind == "error":
            self._last_error = str(msg.get("code") or "")
            logger.warning("Live publication %s: the host says %s", self.pub_id, self._last_error)
        return False

    async def _on_binary(self, data: bytes) -> None:
        if not data or data[0] != T_FRAME or len(data) < 9:
            return  # the snapshot (0x03) is ours; presence (0x02) is not used here
        seq = int.from_bytes(data[1:9], "big")
        env = data[9:]
        try:
            kind, _ = plf.unseal(self.key, self.room, env)
        except plf.PubLiveFormatError:
            self.dropped += 1
        else:
            if kind is plf.Kind.COMMENT:
                try:
                    await self._on_reader_frame(env, seq)
                except Exception:
                    logger.warning(
                        "Live publication %s: a reader's frame failed", self.pub_id, exc_info=True
                    )
            else:
                self.dropped += 1  # only this bridge signs VERSION, THREADS and META
        self._advance(seq)

    async def _on_reader_frame(self, env: bytes, seq: int) -> None:
        """A reader's COMMENT envelope (`seq` in the room's log). Task 11 opens it
        (pub_live_format.open_box with the row's box key) and applies the op; until then it is
        ignored."""

    def _advance(self, seq: Any) -> None:
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > self.seq:
            self.seq = seq
            _write_room(self.app, self.pub_id, self.room, seq)

    # -- what we send --

    def _stamp(self) -> str:
        """`at` for THREADS and META: now, to the millisecond, always later than the last."""
        now = datetime.now(UTC)
        now = now.replace(microsecond=now.microsecond // 1000 * 1000)
        if self._last_at is not None and now <= self._last_at:
            now = self._last_at + timedelta(milliseconds=1)
        self._last_at = now
        return _at_text(now)

    def send_version(self, frame: VersionFrame) -> None:
        self.version = frame
        if self.connected.is_set():
            self.outq.put_nowait(frame)

    def send_meta(self, now: float | None = None) -> MetaFrame | None:
        follow, comments = _switches(self.row)
        self._last_meta = (follow, comments)
        self._meta_at = time.monotonic() if now is None else now
        if not self.connected.is_set():
            return None  # said at the next join
        frame = MetaFrame(comments=comments, follow=follow, at=self._stamp())
        self.outq.put_nowait(frame)
        return frame

    def _threads_frame(self) -> ThreadsFrame:
        from ..models.publication import PublicationSelection

        sel = PublicationSelection.model_validate(self.row["selection"])
        threads = threads_for_readers(self.app, self.session_id, sel)
        return ThreadsFrame(threads=threads, at=self._stamp())

    async def send_threads(self) -> None:
        """The readers' threads changed (task 11): a THREADS frame to the readers in the room,
        and the same as the room's snapshot for the ones who join later."""
        frame = await asyncio.to_thread(self._threads_frame)
        if self.connected.is_set():
            self.outq.put_nowait(frame)
        await self.snapshot(frame)

    async def snapshot(self, frame: ThreadsFrame | None = None) -> bool:
        """Post a signed THREADS as the room's snapshot; the host drops its log up to the
        last seq applied here. META follows (a joiner past the snapshot must still hear it).
        False (logged) when the host did not take it."""
        from .publishing import PublishError

        if not self.room:
            return False
        async with self._snap_lock:
            since = self.seq
            if frame is None:
                frame = await asyncio.to_thread(self._threads_frame)
            try:
                resp = await _http(
                    self.app,
                    "PUT",
                    self.row["host_url"],
                    f"/live/{self.room}/snapshot",
                    self.row["token"],
                    content=self._seal(frame),
                    headers={"X-Room-Since": str(max(0, since))},
                )
            except PublishError as e:
                logger.info("Live publication %s: snapshot not posted: %s", self.pub_id, e)
                return False
            if resp.status_code >= 300:
                logger.info(
                    "Live publication %s: snapshot not posted: %s", self.pub_id, _refusal(resp)
                )
                return False
            self._snapshot_at = time.monotonic()
            self._since_snapshot = False
        self._after_snapshot = self.send_meta()
        return True

    # -- housekeeping --

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Live publication %s: housekeeping failed", self.pub_id, exc_info=True
                )

    async def tick(self, now: float | None = None) -> None:
        """One pass: the row looked at again (unpublished, expired, switched off, moved: the
        bridge retires; the switches changed: META), META as the heartbeat, the room's expiry
        moved, the log pruned by a snapshot when frames went out since the last one."""
        from .publishing import PublishError

        now = time.monotonic() if now is None else now
        current = self.app.repo.get_publication(self.pub_id)
        if not wanted(current):
            stays = current is not None and not current.get("unpublished_at")
            same = stays and current.get("host_url") == self.row.get("host_url")
            self.bridges.spawn(self.bridges.retire(self.row, clear_pointer=same))
            return
        if not _origin_here(self.app, current):
            self.bridges.spawn(self.stop())
            return
        if current.get("host_url") != self.row.get("host_url"):
            self.bridges.spawn(self.bridges.retire(self.row))
            return  # the next publish (or a restart) bridges it on its new host
        self.update_row(current)
        if _switches(current) != self._last_meta or now - self._meta_at >= META_EVERY:
            self.send_meta(now)
        if self.room and not self._lookup and now - self._extended_at >= EXTEND_EVERY:
            try:
                code = await self._put_room(self.room)
            except PublishError as e:
                logger.info("Live publication %s: room expiry not moved: %s", self.pub_id, e)
            else:
                if code in (200, 201):
                    self._extended_at = now
        if (
            self._since_snapshot
            and self.connected.is_set()
            and now - self._snapshot_at >= SNAPSHOT_EVERY
        ):
            if await self.snapshot():
                self._snapshot_at = now
