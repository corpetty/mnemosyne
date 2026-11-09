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
room when the host says this one is gone (4410). Readers' frames (COMMENT, task 11) go to
`_on_reader_frame`: opened with the row's box key, the reader's signature checked, then the op
(create, reply, edit, delete) applied through services/comments.py as `reader:<rid>`, with the
anchor rules of C8, the caps (MAX_TEXT, MAX_NAME, RATE_OPS in RATE_WINDOW), the replay rule
(`readers.json` beside the publication's files) and nothing while comments are off. A passage
of the published notes (M4) becomes a `notes` comment whose mark the bridge puts on the real
text itself (`_create_on_notes`: services/notes_anchor.py finds it, shared_notes marks it under
the notes lock, off the event loop); META `notes` says when that can be done. The
readers' threads (`threads_for_readers`) go back as THREADS, debounced, after every `comments`
event of the meeting and every change of the selection. With `moderate` on (task 13b) a reader's
comment reaches the other readers once the owner approved it (`comments.approve`); its writer's
page is told only its ids (THREADS `pending`, per rid) and shows the text it kept itself; the
owner's Hide keeps a comment from readers whatever `moderate` says. Every outgoing frame is a
`VersionFrame`, `ThreadsFrame` or `MetaFrame` signed with the row's signing key
(pub_live_format.sign_frame).
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
from ..config import hosts_team
from . import pub_live_format as plf
from .room_connection import (
    CLOSE_BAD_TOKEN,
    CLOSE_DELETED,
    CLOSE_ENDED,
    CLOSE_FULL,
    CLOSE_LAGGED,
    CLOSE_TOO_LARGE,
    RETRY_BACKOFF,
    T_NOTES,
    CloseAction,
    Decision,
    ReadAction,
    RoomConnection,
    RoomHooks,
    WelcomeAction,
)

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..jobs import Job, JobContext
    from ..models.comment import Comment, CommentCreate
    from ..models.publication import PublicationSelection
    from ..models.session import Session
    from .notes_anchor import Locate

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
        from .publishing import PublishError, _dt, _now, check_live, served_by

        app = self.ctx
        row = app.repo.live_publication(session_id)
        if row is None or not _follows(row):
            return None
        if row.get("expires_at") and _dt(row["expires_at"]) <= _now():
            return None  # the sweep buries it; an automatic version would make a new link
        session = app.repo.get(session_id)
        if session is None or is_foreign(session):
            return None
        if served_by(app, row) is not None:
            return None
        try:
            sel = PublicationSelection.model_validate(row["selection"])
            check_live(session, sel)
        except (PublishError, ValueError):
            return None
        who = self._owner(session)
        if who is _NOBODY or not _allowed_for(app, who):
            return None
        return row, session, sel, who

    def _owner(self, session: Session) -> Any:
        """The principal the job runs as: the meeting's owner as a user of this backend (so a
        team server's rules, `check_may_publish`, hold), unrestricted on a desktop whose
        meetings have no such owner (as its own requests are), `_NOBODY` on a team server
        whose owner is gone or disabled. A meeting with no owner on a computer that hosts a team
        is its owner's (`_publisher`)."""
        return _publisher(self.ctx, session)

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

T_FRAME = T_NOTES  # the wire's tags and close codes are room_connection.py's

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
    """The readers' threads (`threads_for_readers`, plus "removed" stand-ins); also the
    room's snapshot. The newest `at` wins.

    `pending` (approval on, task 13b): per reader (rid), the ids of that reader's own comments
    still waiting for the owner's approval, roots and replies. Ids only, never a word of what
    they say: every reader receives THREADS, and the writer's page keeps its own text.

    `blocked` (M4): the rids the owner turned comments off for, sorted. Ids only, like
    `pending`: every rid is already in every comment of THREADS. A blocked reader's page says
    so; the others' pages ignore it. An older owner sends none."""

    KIND: ClassVar[plf.Kind] = plf.Kind.THREADS
    threads: list[dict[str, Any]]
    pending: dict[str, list[str]] = {}
    blocked: list[str] = []
    at: str


class MetaFrame(_Frame):
    """The switches as readers need them, and the room's heartbeat. The newest `at` wins.

    `notes` (M4): readers may comment on the published notes: comments on, the notes published
    and not empty, and a sidecar here that marks the passage (`PubBridge.notes_ok`). An older
    owner sends no `notes`: the viewer reads it as false."""

    KIND: ClassVar[plf.Kind] = plf.Kind.META
    comments: bool
    follow: bool
    notes: bool
    at: str


FRAMES: dict[plf.Kind, type[_Frame]] = {
    plf.Kind.VERSION: VersionFrame,
    plf.Kind.THREADS: ThreadsFrame,
    plf.Kind.META: MetaFrame,
}


# ---- readers' comments (task 11) ------------------------------------------------------------

READER_PREFIX = "reader:"  # a reader's comments: author id "reader:<rid>", name "<name> (reader)"
MAX_TEXT = 4000  # characters (code points) of a reader's comment
MAX_NAME = 80  # characters of a reader's name (guest_notes.MAX_NAME)
MAX_CONTEXT = 200  # characters of a body anchor's prefix or suffix (the viewer sends 32)
RATE_OPS = 20  # comment operations a reader may make ...
RATE_WINDOW = 600.0  # ... in this many seconds; more are dropped
# ... and this many across all readers of the publication in the same window: a reader who
# clears their storage is a new rid with a new budget (C7), so the per-reader cap alone is none.
RATE_TOTAL = 60
MAX_BLOCKED = 200  # rids THREADS names (the newest blocks); a few in practice
THREADS_DEBOUNCE = 2.0  # seconds: comments that change together make one THREADS
META_NUDGE = 30.0  # seconds: at most one META in this time for ops sent while comments are off
ON_QUOTE = 280  # characters of the quote in a thread kept on the meeting as a whole ("On: …")
MAX_PENDING_EACH = 50  # waiting comment ids THREADS names per reader (the newest) ...
MAX_PENDING = 2000  # ... and in all
MAX_THREADS_BYTES = 1_500_000  # a THREADS stays well inside the host's 2 MiB frame
OPS = ("create", "reply", "edit", "delete")
OP_TARGETS = ("", "body", "decision", "question", "item", "quote", "notes")
ITEM_PARTS = {"decision": "decisions", "question": "questions", "item": "action_items"}
NO_ANCHOR: dict[str, Any] = {"target": "", "target_id": None, "quote_id": None, "quote": None}


class _Shown:
    """What the publication shows of the meeting under a selection (`build_content`), and the
    names and text rules its manifest went through (publishing.Redaction, the same class), for
    the comments readers see."""

    def __init__(self, app: AppContext, session: Session, sel: PublicationSelection) -> None:
        from .publishing import Redaction, build_content

        self.sel = sel
        self.app, self.session_id = app, session.id
        self.content, self.specs = build_content(app, session, sel)
        # The manifest's own names and text rules (build_content uses the same class).
        self.redaction = Redaction(session, sel)
        self.names = self.redaction.names
        c = self.content
        self.items: dict[str, dict[str, str]] = {
            "decision": {d["id"]: d["text"] for d in c["decisions"]},
            "question": {q["id"]: q["text"] for q in c["questions"]},
            "item": {a["id"]: a["text"] for a in c["action_items"]},
        }
        self.quotes: dict[str, str] = {q["id"]: q["text"] for q in c["quotes"]}
        self._marks: tuple[str, dict] | None = None
        self._marks_read = False

    def text(self, value: str) -> str:
        return self.redaction.text(value)

    def notes_marks(self) -> tuple[str, dict] | None:
        """The notes' plain text and comment ranges as last folded (read once)."""
        from .shared_notes import unrestricted

        if not self._marks_read:
            with unrestricted():
                self._marks = self.app.repo.notes_marks(self.session_id)
            self._marks_read = True
        return self._marks

    def notes_quote(self, root: Comment) -> str | None:
        """A notes thread's quote as the publication shows it: the marked text of the last
        fold through the names and text rules, else (detached, no fold yet) its own quote the
        same way; None for a deleted one."""
        from .comments import place_notes

        if root.deleted:
            return None
        marks = self.notes_marks()
        found, _ = place_notes(root.id, root.quote, marks)
        if found is not None and marks is not None:
            marked = marks[0][found[0] : found[1]].replace("\x1f", "\t")
            return self.text(marked) or None
        return self.text(root.quote) or None

    def anchor(self, root: Comment) -> dict[str, Any] | None:
        """Where a reader thread is, as THREADS says it ({target, target_id, quote_id, quote}),
        or None when it is on a part the publication does not show (a quote is only ever the
        manifest's own text)."""
        target = root.target
        if target == "":
            return dict(NO_ANCHOR)
        if target == "segment":
            idx = root.segment_idx
            if idx is None:
                return None
            for spec in self.specs:
                if spec["first"] <= idx <= spec["last"]:
                    return {
                        **NO_ANCHOR,
                        "target": "segment",
                        "quote_id": spec["id"],
                        "quote": self.quotes.get(spec["id"]),
                    }
            return None  # a line no published quote covers
        if target == "body":
            if not self.sel.summary:
                return None
            quote = None if root.detached or root.deleted else self.text(root.quote) or None
            return {**NO_ANCHOR, "target": "body", "quote": quote}
        if target in ITEM_PARTS:
            if not getattr(self.sel, ITEM_PARTS[target]):
                return None
            return {
                **NO_ANCHOR,
                "target": target,
                "target_id": root.target_id or None,
                "quote": self.items[target].get(root.target_id),
            }
        if target == "notes":
            if not self.sel.notes or not self.content["notes"]:
                return None
            return {**NO_ANCHOR, "target": "notes", "quote": self.notes_quote(root)}
        return None  # anything readers cannot write on

    def comment(self, c: Comment, anchor: dict[str, Any]) -> dict[str, Any]:
        """A comment as readers see it: a reader's as they wrote it, with their rid; the owner's
        side (replies) through the publication's names and text rules."""
        reader = c.author_id.startswith(READER_PREFIX)
        if reader:
            author, text = c.author, c.text
        else:
            name = (c.author or "").strip()
            # "You" / "Me" (the desktop's defaults) would read as the reader themself.
            plain = name and name.casefold() not in ("you", "me")
            author = (self.names.name(name) if plain else "") or "Owner"
            text = self.text(c.text)
        return {
            "id": c.id,
            "parent_id": c.parent_id or None,
            "author": author,
            "rid": c.author_id[len(READER_PREFIX) :] if reader else None,
            "text": "" if c.deleted else text,
            "anchor": anchor,
            "created_at": _at_text(c.created_at),
            "updated_at": _at_text(c.updated_at),
            "resolved": c.resolved_at is not None,
            "deleted": c.deleted,
            "detached": bool(c.detached),
            "changed": bool(c.changed),
        }


def _reader_view(
    app: AppContext, session_id: str, sel: PublicationSelection
) -> tuple[list[dict], set[str], dict[str, list[str]]]:
    """(threads readers see, ids of every comment the meeting has now, the waiting ones).

    The owner's Hide keeps a comment from readers whatever the selection says: a hidden first
    comment takes its thread, a hidden reply goes alone. With `comments` and `moderate` on, a
    reader's comment (first or reply) that the owner has not approved is left out too, and a
    first comment's whole thread with it, unless it was last written before `moderated_since`
    (when the switch went on: everyone could see those). The ones left out for want of approval
    are listed per reader as `waiting` ({rid: [comment ids]}, ids only), those of a thread that
    would otherwise show."""
    from . import comments
    from .shared_notes import unrestricted

    with unrestricted():
        session = app.repo.get(session_id)
        if session is None or session.shared_from:
            return [], set(), {}
        listed = comments.threads(app, session_id)
        ids = {c.id for c in app.repo.list_comments(session_id)}
        shown = _Shown(app, session, sel)
    moderating = sel.comments and sel.moderate
    since = _parse(sel.moderated_since) if moderating else None

    def unapproved(c: Comment) -> bool:
        if not moderating or not c.author_id.startswith(READER_PREFIX):
            return False
        if c.approved_at is not None:
            return False
        # An edit counts as a new comment: `updated_at`, not `created_at`.
        return since is None or c.updated_at.astimezone(UTC) >= since

    out = []
    waiting: dict[str, list[str]] = {}
    for t in listed.threads:
        root = t.root
        if root.parent_id or not root.author_id.startswith(READER_PREFIX):
            continue  # the owner's own threads never leave the app
        if root.hidden_at is not None:
            continue
        anchor = shown.anchor(root)
        if anchor is None:
            continue
        replies = [r for r in t.replies if r.hidden_at is None]
        for c in [root, *replies]:
            if unapproved(c) and not c.deleted:
                waiting.setdefault(c.author_id[len(READER_PREFIX) :], []).append(c.id)
        if unapproved(root):
            continue
        visible = [r for r in replies if not unapproved(r)]
        out.append(
            {
                "root": shown.comment(root, anchor),
                "replies": [shown.comment(r, dict(NO_ANCHOR)) for r in visible],
            }
        )
    return out, ids, _cap_waiting(waiting)


def _cap_waiting(waiting: dict[str, list[str]]) -> dict[str, list[str]]:
    """At most MAX_PENDING_EACH (the newest) per reader and MAX_PENDING in all."""
    out: dict[str, list[str]] = {}
    left = MAX_PENDING
    for rid, found in waiting.items():
        keep = found[-min(MAX_PENDING_EACH, left) :] if left > 0 else []
        if keep:
            out[rid] = keep
            left -= len(keep)
    return out


def threads_for_readers(app: AppContext, session_id: str, sel: PublicationSelection) -> list:
    """The threads readers see, oldest first: only threads a reader started, with every reply
    in them (the owner's through the selection's names and identifier rules, its author as
    `Names.name` gives it, else "Owner"), and none on a part the selection does not show, none
    the owner hid, and, with `moderate` on, none not approved yet (`_reader_view`). Each
    comment is {id, parent_id, author, rid, text, anchor: {target, target_id, quote_id, quote},
    created_at, updated_at, resolved, deleted, detached, changed}; `rid` names a reader's own
    (None for the owner's side)."""
    return _reader_view(app, session_id, sel)[0]


def pending_for_readers(
    app: AppContext, session_id: str, sel: PublicationSelection
) -> dict[str, list[str]]:
    """{rid: ids of that reader's comments waiting for the owner's approval}: what THREADS says
    to every reader so each page can show its own (ids only; see `ThreadsFrame`)."""
    return _reader_view(app, session_id, sel)[2]


@dataclass(frozen=True)
class _Op:
    """A reader's COMMENT op, checked: the shape task 12 fixed, within the caps."""

    kind: str
    id: str
    parent_id: str
    text: str
    name: str
    at: datetime
    anchor: dict[str, str]


def _op_str(value: Any, limit: int) -> str | None:
    """A string field of an op ("" for null), None when it is not one or too long."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        return None
    return value


def _parse_op(obj: dict) -> _Op | None:
    """The op, or None when it is not one a reader may send (unknown kind, bad id or time, no
    text, more than MAX_TEXT characters, an anchor that is not one of OP_TARGETS)."""
    from .comments import COMMENT_ID

    kind, cid, at = obj.get("op"), obj.get("id"), obj.get("at")
    if kind not in OPS or not isinstance(cid, str) or not COMMENT_ID.fullmatch(cid):
        return None
    when = _parse(at) if isinstance(at, str) else None
    if when is None:
        return None
    name = obj.get("name")
    name = name if isinstance(name, str) else ""
    text = ""
    if kind != "delete":
        raw = obj.get("text")
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text or len(text) > MAX_TEXT:
            return None
    parent = ""
    if kind == "reply":
        parent = obj.get("parent_id")
        if not isinstance(parent, str) or not COMMENT_ID.fullmatch(parent):
            return None
    anchor: dict[str, str] = {}
    if kind == "create":
        raw = obj.get("anchor")
        if not isinstance(raw, dict) or raw.get("target") not in OP_TARGETS:
            return None
        limits = {"target_id": 64, "quote_id": 64, "exact": MAX_TEXT}
        limits |= {"prefix": MAX_CONTEXT, "suffix": MAX_CONTEXT}
        for field, limit in limits.items():
            value = _op_str(raw.get(field), limit)
            if value is None:
                return None
            anchor[field] = value
        anchor["target"] = raw["target"]
    return _Op(kind, cid, parent, text, name, when, anchor)


def _reader_name(name: str) -> str:
    clean = " ".join("".join(ch for ch in name if ch.isprintable()).split())
    return f"{clean[:MAX_NAME].strip() or 'Reader'} (reader)"


def _on_meeting(text: str, quote: str) -> str:
    """A thread whose place cannot be found, kept on the meeting as a whole with what it was
    about in its text."""
    from .comments import excerpt

    quote = excerpt(quote, ON_QUOTE)
    return f"On: “{quote}”\n\n{text}" if quote else text


def _placed(shown: _Shown, op: _Op) -> tuple[CommentCreate, str] | None:
    """(the comment to create, the quote to keep it on the meeting with if its place is not
    found) for a reader's new thread, or None when it is on nothing the publication shows (C8:
    an item by its block id, a quote by its transcript lines, the summary text by quote, the
    notes by quote (M4: the bridge marks the passage, `_create_on_notes`), the meeting as a
    whole)."""
    from ..models.comment import CommentCreate

    a, text, sel = op.anchor, op.text, shown.sel
    target = a["target"]
    if target in ITEM_PARTS:
        if not getattr(sel, ITEM_PARTS[target]):
            return None
        quote = shown.items[target].get(a["target_id"])
        if quote is None:
            return None
        return CommentCreate(target=target, target_id=a["target_id"], text=text), quote
    if target == "quote":
        spec = next((s for s in shown.specs if s["id"] == a["quote_id"]), None)
        if spec is None:
            return None
        req = CommentCreate(target="segment", idx=spec["first"], idx_to=spec["last"], text=text)
        return req, shown.quotes.get(spec["id"], "")
    if target == "body" and a["exact"].strip():
        if not sel.summary or not shown.content["summary"]:
            return None
        exact = a["exact"]
        req = CommentCreate(
            target="body", exact=exact, prefix=a["prefix"], suffix=a["suffix"], text=text
        )
        return req, exact
    if target == "notes" and a["exact"].strip():
        if not sel.notes or not shown.content["notes"]:
            return None
        exact = a["exact"]
        return CommentCreate(target="notes", exact=exact, text=text), exact
    return CommentCreate(text=text), ""  # the meeting as a whole (or a passage of nothing)


def _readers_file(app: AppContext, pub_id: str) -> Path:
    from .publishing import pub_dir

    return pub_dir(app, pub_id) / "readers.json"


def _read_readers(app: AppContext, pub_id: str) -> tuple[dict[str, str], dict[str, dict]]:
    """(the `at` of the last op applied per comment id, the comments readers were shown:
    what a "removed" stand-in needs) kept beside the publication's files."""
    try:
        data = json.loads(_readers_file(app, pub_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    ops, sent = data.get("ops"), data.get("sent")
    ops = {k: v for k, v in ops.items() if isinstance(v, str)} if isinstance(ops, dict) else {}
    sent = {k: v for k, v in sent.items() if isinstance(v, dict)} if isinstance(sent, dict) else {}
    return ops, sent


def _write_readers(app: AppContext, pub_id: str, ops: dict, sent: dict) -> None:
    from .publishing import read_meta

    path = _readers_file(app, pub_id)
    if not path.parent.is_dir() or (read_meta(app, pub_id) or {}).get("gone"):
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ops": ops, "sent": sent}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("Live publication %s: could not keep its readers' ops", pub_id)


def _removed(stub: dict) -> dict:
    """A comment readers were shown that is gone from the meeting (deleted, or a thread
    deleted through): "removed", with nothing of what it said or was on."""
    anchor = stub.get("anchor") if isinstance(stub.get("anchor"), dict) else NO_ANCHOR
    return {
        **stub,
        "text": "",
        "anchor": {**NO_ANCHOR, **anchor, "quote": None},
        "resolved": False,
        "deleted": True,
        "detached": False,
        "changed": False,
    }


def _trim(threads: list[dict], reserve: int = 0) -> list[dict]:
    """Oldest threads left out while the THREADS would be larger than MAX_THREADS_BYTES.
    `reserve`: bytes the rest of the frame takes (`blocked` stays whole, a few ids)."""
    sizes = [len(json.dumps(t, ensure_ascii=False).encode()) for t in threads]
    total, start = sum(sizes) + reserve, 0
    while total > MAX_THREADS_BYTES and start < len(threads):
        total -= sizes[start]
        start += 1
    return threads[start:]


# -- rows, rooms, time --


def _blocked_ids(row: dict | None) -> list[str]:
    """The rids blocked on the row (`blocked_readers` entries that are `on`), sorted; the newest
    MAX_BLOCKED when there are more."""
    entries = (row or {}).get("blocked_readers") or {}
    on = [(str(e.get("at") or ""), rid) for rid, e in entries.items() if e.get("on")]
    return sorted(rid for _, rid in sorted(on)[-MAX_BLOCKED:])


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


def _publisher(app: AppContext, session: Session) -> Any:
    """Who an automatic version of this meeting runs as: its owner as a user of this backend,
    which on a computer that hosts a team is also the owner of a meeting with none (older ones,
    made before its owner was stamped on them); unrestricted (None) on a desktop whose meetings
    have no such owner; `_NOBODY` on a team server whose owner is gone or disabled."""
    st = app.settings
    owner_id = session.owner_id or (st.team_owner_id if hosts_team(st) else "")
    user = app.users.get(owner_id) if owner_id else None
    if user is not None and not user.disabled:
        return access.Principal(user.id, user.name, user.role)
    return _NOBODY if st.team_mode else None


def _allowed_for(app: AppContext, who: Any) -> bool:
    """`publishing.allowed` as `who` (the background watcher has no signed-in caller of its own)."""
    from .publishing import allowed

    token = access.current.set(who)
    try:
        return allowed(app)
    finally:
        access.current.reset(token)


def _origin_here(app: AppContext, row: dict) -> bool:
    """This machine bridges the publication: publishing is on and the meeting was recorded
    here (not on the other computer of a library-sync pair, not shared with us)."""
    from .origin import is_foreign
    from .shared_notes import unrestricted

    with unrestricted():
        session = app.repo.get(row["session_id"])
    if session is None or is_foreign(session) or session.shared_from:
        return False
    who = _publisher(app, session)
    return who is not _NOBODY and _allowed_for(app, who)


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


def row_changed(app: AppContext, row: dict) -> None:
    """A write to the row that is not a publish (the block list): the running bridge looks at
    it now, so a change readers must hear (THREADS `blocked`) does not wait for the tick."""
    bridges = _bridges(app)
    bridge = bridges.get(row["id"]) if bridges is not None else None
    if bridge is not None:
        bridge.update_row(row)


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
        self.outq: asyncio.Queue[_Frame] = asyncio.Queue()
        # The socket: `conn.unacked` holds the frames sent and not acknowledged yet.
        self.conn = RoomConnection(
            _PubHooks(self),
            outq=self.outq,
            max_message=MAX_MESSAGE,
            backoff=lambda: ROOM_BACKOFF,  # (tests patch the module's)
            join_timeout=JOIN_TIMEOUT,
            label=f"Live publication {row['id']}",
        )
        self.version: VersionFrame | None = None  # the newest, until the room acknowledges it
        self.task: asyncio.Task | None = None
        self.running = False
        self.closing = False  # retiring: the host's word that the room went is no news
        self.dropped = 0  # frames from the room that are not readers' comments, or do not open
        self._refused: set[str] = set()  # rooms whose token the host refused: never again
        self._lookup = True  # look the room up on the host before the next join
        self._last_at: datetime | None = None
        self._last_meta: tuple[bool, bool, bool] | None = None  # (follow, comments, notes)
        self._meta_at = float("-inf")
        self._extended_at = float("-inf")
        self._snapshot_at = float("-inf")
        self._since_snapshot = False  # frames sent since the last snapshot
        self._after_snapshot: _Frame | None = None  # the META sent right after it
        self._snap_lock = asyncio.Lock()
        # Readers' comments (task 11): the `at` of the last op applied per comment id, the
        # comments readers were shown (for "removed"), each reader's recent ops, the threads
        # last sent, the pending THREADS, the bus subscription.
        self._ops, self._sent = _read_readers(self.app, self.pub_id)
        self._rates: dict[str, deque[float]] = {}
        self._rates_all: deque[float] = deque()  # every reader's, for RATE_TOTAL
        self._last_threads: list[dict] | None = None
        self._last_pending: dict[str, list[str]] | None = None
        self._last_blocked: list[str] | None = None
        self._threads_task: asyncio.Task | None = None
        self._sub: asyncio.Queue | None = None
        self.clock: Callable[[], float] = time.monotonic  # the rate window's (tests swap it)
        self.outcomes: dict[str, int] = {}  # what became of readers' ops, by outcome (tests)
        # Notes comments (M4): a sidecar that marks passages answered (probed off the loop at
        # start and every tick), and the marks `_take` left to make once it returns.
        self.notes_ok = False
        self._marks_due: list[tuple[str, Locate, tuple[str, str]]] = []

    @property
    def readers(self) -> int:
        return max(0, self.present - 1)

    @property
    def connected(self) -> asyncio.Event:
        return self.conn.connected

    @property
    def unacked(self) -> deque[_Frame]:
        return self.conn.unacked

    @property
    def joins(self) -> int:
        return self.conn.joins  # successful joins (tests)

    @property
    def ws(self) -> Any:
        return self.conn.ws

    @ws.setter
    def ws(self, value: Any) -> None:
        self.conn.ws = value

    # -- life --

    def start(self) -> None:
        if self.task is not None:
            return
        self.running = True
        self.bridges.by_id[self.pub_id] = self
        self._sub = self.app.bus.subscribe()
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
        if self._sub is not None:
            self.app.bus.unsubscribe(self._sub)
            self._sub = None
        task, self._threads_task = self._threads_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if self.bridges.by_id.get(self.pub_id) is self:
            del self.bridges.by_id[self.pub_id]

    def update_row(self, row: dict) -> None:
        """A newer row (a publish, or the tick's look): a switch turned on or off is META at
        once; a new expiry moves the room's at the next tick; another selection may show
        readers other threads (a part unticked takes its threads away): THREADS soon."""
        if row.get("expires_at") != self.row.get("expires_at"):
            self._extended_at = float("-inf")
        selection_changed = row.get("selection") != self.row.get("selection") or (
            _blocked_ids(row) != _blocked_ids(self.row)
        )
        self.row = dict(row)
        if self._last_meta is not None and self._meta_state() != self._last_meta:
            self.send_meta()
        if selection_changed:
            self.threads_soon()

    async def _run(self) -> None:
        ticker = asyncio.create_task(self._ticker())
        events = asyncio.create_task(self._events())
        try:
            await self._probe_notes()  # before the join: its META says it
            await self._connect_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("The live publication bridge of %s failed", self.pub_id, exc_info=True)
        finally:
            ticker.cancel()
            events.cancel()
            await asyncio.gather(ticker, events, return_exceptions=True)
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
        await self.conn.run()

    async def _before_connect(self, attempt: int) -> int | None:
        """The room lookup before a dial. None: dial; an int: waited, loop with that attempt."""
        if self._lookup or not self.room:
            outcome = await self._ensure_room()
            if outcome in ("unsupported", "gone"):
                await asyncio.sleep(RETRY_LATER)
                return attempt
            if outcome != "ok":
                await asyncio.sleep(ROOM_BACKOFF[min(attempt, len(ROOM_BACKOFF) - 1)])
                return attempt + 1
            self._lookup = False
            await self.snapshot()  # a failure is logged; the room still works without
        return None

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

    def _seal(self, frame: _Frame) -> bytes:
        return plf.sign_frame(
            self.key, self.room, frame.KIND, frame.model_dump(mode="json"), self.row["sign_key"]
        )

    async def _on_frame(self, seq: int, env: bytes) -> None:
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
        """A reader's COMMENT envelope (`seq` in the room's log): opened with the row's box key
        and the reader's signature checked before anything else, then the op applied as
        `reader:<rid>` through services/comments.py (`_take`). Never logs what it carries."""
        try:
            ro = plf.open_box(self.key, self.room, env, self.row["box_key"])
        except plf.PubLiveFormatError:
            outcome = "unopened"
        else:
            try:
                outcome = self._take(ro)
            finally:
                due, self._marks_due = self._marks_due, []
            for comment_id, locate, actor in due:
                await self._mark_notes(comment_id, locate, actor)
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        if outcome != "applied":
            logger.info(
                "Live publication %s: a reader's op was not taken (%s)", self.pub_id, outcome
            )

    def _take(self, ro: plf.ReaderOp) -> str:
        """Apply one opened op; returns what became of it: "applied", "off" (comments are
        off: dropped, and META says so again), "bad" (not an op a reader may send, or over a
        cap), "replay" (applied before: a create or reply with a known id, an edit or delete
        not newer than the last op applied on that comment), "not_yours" (an edit or delete of
        a comment that is not this reader's), "rate" (more than RATE_OPS in RATE_WINDOW),
        "refused" (comments.py or the anchor rules said no), "blocked" (the owner turned comments
        off for this reader: nothing is written, whatever the op is); "rate" also covers
        RATE_TOTAL ops across all readers."""
        from . import comments
        from .shared_notes import unrestricted

        if ro.rid != plf.rid_of(ro.reader_pub):
            return "bad"
        current = self.app.repo.get_publication(self.pub_id)
        if current is not None and current != self.row:
            self.update_row(current)
        if ro.rid in _blocked_ids(current or self.row):
            return "blocked"  # before anything is read of it or written for it
        if not wanted(current) or not _switches(current)[1]:
            self._nudge_meta()
            return "off"
        op = _parse_op(ro.op)
        if op is None:
            return "bad"
        last = _parse(self._ops.get(op.id))
        if op.kind in ("create", "reply"):
            if op.id in self._ops:
                return "replay"
        elif last is not None and op.at <= last:
            return "replay"
        actor = (READER_PREFIX + ro.rid, _reader_name(op.name))
        sid = self.session_id
        try:
            with unrestricted():
                session = self.app.repo.get(sid)
                if session is None or session.shared_from:
                    return "refused"
                if op.kind in ("edit", "delete"):
                    c = self.app.repo.get_comment(op.id)
                    if c is None or c.session_id != sid or c.author_id != actor[0]:
                        return "not_yours"
                if not self._rate_ok(ro.rid):
                    return "rate"
                if op.kind == "create":
                    self._create(session, op, actor)
                elif op.kind == "reply":
                    self._reply(session, op, actor)
                elif op.kind == "edit":
                    comments.edit(self.app, sid, op.id, op.text, actor=actor)
                    self._unapprove(op.id)
                else:
                    comments.delete(self.app, sid, op.id, actor=actor)
        except (
            comments.BadComment,
            comments.CommentNotFound,
            access.Forbidden,
            access.Hidden,
            ValueError,
        ):
            return "refused"
        self._ops[op.id] = op.at.isoformat()
        _write_readers(self.app, self.pub_id, self._ops, self._sent)
        return "applied"  # its `comments` event brings the THREADS

    def _unapprove(self, comment_id: str) -> None:
        """A reader changed a comment the owner may have approved: with approval on, the new
        text waits for the owner again (a hidden comment stays hidden)."""
        sel = self._selection()
        c = self.app.repo.get_comment(comment_id)
        if sel.comments and sel.moderate and c is not None and c.approved_at is not None:
            self.app.repo.set_comment_moderation(comment_id, None, c.hidden_at)

    def _selection(self) -> PublicationSelection:
        from ..models.publication import PublicationSelection

        return PublicationSelection.model_validate(self.row["selection"])

    def _create(self, session: Session, op: _Op, actor: tuple[str, str]) -> None:
        """A reader's new thread, where the manifest says it is (C8); a place comments.py
        cannot find (a summary passage with a redacted name, say), or a notes passage on a
        computer that cannot mark one (no sidecar, C5), keeps the thread on the meeting as a
        whole with the quote in its text, never drops it."""
        from ..models.comment import CommentCreate
        from .comments import BadComment, NotesNotShared, create

        shown = _Shown(self.app, session, self._selection())
        placed = _placed(shown, op)
        if placed is None:
            raise BadComment("Not on a part of the publication")
        req, quote = placed
        try:
            if req.target == "notes":
                self._create_on_notes(session, shown, op, req, actor)
            else:
                create(self.app, session.id, req, comment_id=op.id, actor=actor)
        except (BadComment, NotesNotShared):
            if req.target == "":
                raise
            fallback = CommentCreate(text=_on_meeting(op.text, quote))
            create(self.app, session.id, fallback, comment_id=op.id, actor=actor)

    def _create_on_notes(
        self,
        session: Session,
        shown: _Shown,
        op: _Op,
        req: CommentCreate,
        actor: tuple[str, str],
    ) -> None:
        """A reader's thread on the notes (M4, Item 1): made as a web reader's is (its anchor
        is the mark `comment:<id>`, its quote the reader's passage, detached until marked), and
        the mark left for `_on_reader_frame` to put, off the event loop, on the passage found
        in the fold it marks against (`shared_notes.mark_comment_located`). R, the notes as the
        reader read them, is the publication's own notes (`shown`), worked out here, outside
        the notes lock. NotesNotShared when this computer cannot mark one (no sidecar: the
        caller keeps the thread on the meeting)."""
        from .comments import NotesNotShared, create
        from .notes_anchor import as_read, locator

        if not self.notes_ok:  # (comments.create would probe the sidecar on the loop)
            raise NotesNotShared("The notes are not shared on this computer")
        create(self.app, session.id, req, comment_id=op.id, actor=actor)
        rendered = as_read(shown.content["notes"] or "")
        locate = locator(
            self.app, session, shown.sel, op.anchor, shown.redaction, rendered=rendered
        )
        self._marks_due.append((op.id, locate, actor))

    async def _mark_notes(self, comment_id: str, locate: Locate, actor: tuple[str, str]) -> None:
        """Put a reader's notes thread's mark on its passage, as the reader (the frame log says
        who). Not found, or the sidecar failing: the thread stays detached, shown by its
        quote. Marked: a `comments` event, so THREADS carries the marked text."""
        from .comments import _publish
        from .shared_notes import mark_comment_located, unrestricted

        with unrestricted():
            marked = await mark_comment_located(
                self.app, self.session_id, comment_id, locate, actor[0], actor[1]
            )
        if marked:
            _publish(self.app, self.session_id)
        else:
            logger.info("Live publication %s: a reader's notes passage not marked", self.pub_id)

    def _reply(self, session: Session, op: _Op, actor: tuple[str, str]) -> None:
        """A reader's reply: only to a thread readers are shown."""
        from ..models.comment import CommentCreate
        from .comments import BadComment, create

        shown = threads_for_readers(self.app, session.id, self._selection())
        if op.parent_id not in {t["root"]["id"] for t in shown}:
            raise BadComment("No such thread for readers")
        req = CommentCreate(parent_id=op.parent_id, text=op.text)
        create(self.app, session.id, req, comment_id=op.id, actor=actor)

    def _rate_ok(self, rid: str) -> bool:
        now = self.clock()
        recent = self._rates.setdefault(rid, deque())
        for window in (recent, self._rates_all):
            while window and window[0] <= now - RATE_WINDOW:
                window.popleft()
        if len(recent) >= RATE_OPS or len(self._rates_all) >= RATE_TOTAL:
            return False
        recent.append(now)
        self._rates_all.append(now)
        return True

    def _nudge_meta(self) -> None:
        """A reader sent an op while comments are off: their page has not heard it, so META
        again (at most once in META_NUDGE seconds)."""
        if self.connected.is_set() and time.monotonic() - self._meta_at >= META_NUDGE:
            self.send_meta()

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

    def _meta_state(self, row: dict | None = None) -> tuple[bool, bool, bool]:
        """(follow, comments, notes) as META says them: `notes` when comments are on, the
        selection publishes the notes and they are not empty (`build_content`'s rule), and
        this computer can mark a passage (`notes_ok`)."""
        from ..models.publication import PublicationSelection
        from .publishing import _own_notes
        from .shared_notes import unrestricted

        row = self.row if row is None else row
        follow, comments = _switches(row)
        notes = False
        if comments and self.notes_ok:
            sel = PublicationSelection.model_validate(row["selection"])
            if sel.notes:
                with unrestricted():
                    session = self.app.repo.get(self.session_id)
                    notes = (
                        session is not None
                        and not session.shared_from
                        and bool(_own_notes(self.app, session).strip())
                    )
        return follow, comments, notes

    async def _probe_notes(self) -> None:
        """Whether a sidecar here answers `notes doc` (shared_notes.available runs it once per
        binary: off the loop)."""
        from .shared_notes import available

        try:
            self.notes_ok = await asyncio.to_thread(available)
        except Exception:
            logger.warning("Live publication %s: sidecar probe failed", self.pub_id, exc_info=True)
            self.notes_ok = False

    def send_meta(self, now: float | None = None) -> MetaFrame | None:
        follow, comments, notes = self._last_meta = self._meta_state()
        self._meta_at = time.monotonic() if now is None else now
        if not self.connected.is_set():
            return None  # said at the next join
        frame = MetaFrame(comments=comments, follow=follow, notes=notes, at=self._stamp())
        self.outq.put_nowait(frame)
        return frame

    async def _threads(self) -> tuple[list[dict], dict[str, list[str]]]:
        """(the threads readers see now, the comment ids waiting for approval per reader): the
        threads with a "removed" stand-in for every comment they were shown that is gone from
        the meeting (so a deleted thread reads "removed" rather than vanishing), within
        MAX_THREADS_BYTES."""
        threads, ids, waiting = await asyncio.to_thread(
            _reader_view, self.app, self.session_id, self._selection()
        )
        present: dict[str, dict] = {}
        for t in threads:
            present[t["root"]["id"]] = t
            for c in [t["root"], *t["replies"]]:
                self._sent[c["id"]] = {
                    k: c[k] for k in ("id", "parent_id", "author", "rid", "created_at")
                } | {"updated_at": c["updated_at"], "anchor": {**c["anchor"], "quote": None}}
        shown = {c["id"] for t in threads for c in [t["root"], *t["replies"]]}
        gone = [s for cid, s in self._sent.items() if cid not in shown and cid not in ids]
        out = [dict(t, replies=list(t["replies"])) for t in threads]
        by_root = {t["root"]["id"]: t for t in out}
        for stub in sorted(gone, key=lambda s: str(s.get("created_at", ""))):
            parent = stub.get("parent_id")
            if not parent:
                by_root[stub["id"]] = {"root": _removed(stub), "replies": []}
                out.append(by_root[stub["id"]])
        for stub in sorted(gone, key=lambda s: str(s.get("created_at", ""))):
            parent = stub.get("parent_id")
            if parent and parent in by_root:
                thread = by_root[parent]
                thread["replies"].append(_removed(stub))
                thread["replies"].sort(key=lambda c: c["created_at"])
        out.sort(key=lambda t: t["root"]["created_at"])
        _write_readers(self.app, self.pub_id, self._ops, self._sent)
        blocked = _blocked_ids(self.row)
        return _trim(out, len(json.dumps(blocked).encode())), waiting

    async def _threads_frame(self) -> ThreadsFrame:
        threads, waiting = await self._threads()
        blocked = _blocked_ids(self.row)
        self._last_threads, self._last_pending, self._last_blocked = threads, waiting, blocked
        return ThreadsFrame(threads=threads, pending=waiting, blocked=blocked, at=self._stamp())

    def threads_soon(self) -> None:
        """THREADS in THREADS_DEBOUNCE seconds (whatever else changes meanwhile goes with it)."""
        if self._threads_task is not None or not self.running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def later() -> None:
            await asyncio.sleep(THREADS_DEBOUNCE)
            self._threads_task = None  # a change from here on makes another
            try:
                await self.send_threads()
            except Exception:
                logger.warning("Live publication %s: THREADS not sent", self.pub_id, exc_info=True)

        self._threads_task = loop.create_task(later(), context=contextvars.Context())

    async def _events(self) -> None:
        """The meeting's `comments` events (a reader's op, the owner's reply, delete or
        resolve, sync): THREADS, debounced."""
        q = self._sub
        if q is None:
            return
        while True:
            batch = [await q.get()]
            while not q.empty():
                batch.append(q.get_nowait())
            if any(
                e.get("type") == "comments" and e.get("session_id") == self.session_id
                for e in batch
            ):
                self.threads_soon()

    async def send_threads(self, *, force: bool = False) -> None:
        """The readers' threads may have changed: unless they are what was sent last, a
        THREADS frame to the readers in the room, and the same as the room's snapshot for the
        ones who join later."""
        threads, waiting = await self._threads()
        blocked = _blocked_ids(self.row)
        if (
            not force
            and threads == self._last_threads
            and waiting == self._last_pending
            and blocked == self._last_blocked
        ):
            return  # (a change to the owner's own threads, say: nothing readers see)
        self._last_threads, self._last_pending, self._last_blocked = threads, waiting, blocked
        frame = ThreadsFrame(threads=threads, pending=waiting, blocked=blocked, at=self._stamp())
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
                frame = await self._threads_frame()
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
        await self._probe_notes()  # a sidecar installed or gone since: META says so
        self.update_row(current)
        if self._meta_state() != self._last_meta or now - self._meta_at >= META_EVERY:
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


class _PubHooks(RoomHooks):
    """What differs from the guest bridge's connection (room_connection.py): the room is looked
    up on the host before a dial and may be replaced when the host says it is gone; frames are
    signed when sent and everything queued or unacknowledged is said again at the next join
    (META anew, the newest VERSION); the sequence is kept in `room.json`."""

    def __init__(self, bridge: PubBridge) -> None:
        self.b = bridge

    async def before_connect(self, attempt: int) -> int | None:
        return await self.b._before_connect(attempt)

    def url(self) -> str:
        from .guest_notes import ws_url

        b = self.b
        return ws_url(b.row["host_url"], b.room)

    def join_message(self) -> dict:
        b = self.b
        return {"type": "join", "token": b.room_token, "since": b.seq}

    async def on_welcome(self, welcome: dict) -> WelcomeAction:
        if welcome.get("ended"):
            return WelcomeAction.LEAVE_ROOM  # ours are never ended: someone else's, or broken
        b = self.b
        b.present = int(welcome.get("present") or 0)
        b.reason = ""
        return WelcomeAction.CONTINUE

    def on_connected(self) -> None:
        self.b._joined()

    def prepare(self, item: _Frame) -> bytes:
        b = self.b
        env = b._seal(item)
        b.unacked.append(item)
        if item is not b._after_snapshot:
            b._since_snapshot = True  # (the META that follows a snapshot is no news)
        return bytes([T_FRAME]) + env

    def on_ack(self, done: _Frame) -> None:
        if done is self.b.version:
            self.b.version = None

    def advance(self, seq: Any) -> None:
        self.b._advance(seq)

    def on_count(self, present: int) -> None:
        self.b.present = present

    async def on_text(self, msg: dict) -> ReadAction | None:
        return ReadAction.LEAVE_ROOM if msg.get("type") == "ended" else None

    async def on_frame(self, seq: int, envelope: bytes) -> None:
        await self.b._on_frame(seq, envelope)

    def on_unreachable(self, error: BaseException) -> None:
        self.b.reason = "Could not reach the publish host's live room"
        logger.info("Live publication %s: cannot reach its room: %s", self.b.pub_id, error)

    def on_refused(self, status: int) -> None:
        self.b.reason = f"The publish host refused the live room ({status})"

    def on_disconnected(self) -> None:
        self.b._disconnected()

    def stop_requested(self) -> bool:
        return self.b.closing

    async def on_closed(
        self,
        code: int | None,
        status: int | None,
        last_error: str,
        attempt: int,
        unacked: deque,
    ) -> Decision:
        b = self.b
        if code in (CLOSE_DELETED, CLOSE_ENDED, CLOSE_BAD_TOKEN) or status in (404, 410):
            logger.info("Live publication %s: its room is gone (%s)", b.pub_id, code)
            if code == CLOSE_BAD_TOKEN:
                b._refused.add(b.room)
            b._lose_room()
            if attempt == 0:  # at once, the first time
                return Decision(CloseAction.RETRY_NOW, 1)
        elif code == CLOSE_TOO_LARGE and last_error == "log_full":
            await b.snapshot()  # prunes the log (X-Room-Since), then rejoin
        elif code == CLOSE_LAGGED:
            return Decision(CloseAction.RETRY_NOW, 0)
        elif code == CLOSE_FULL:
            b.reason = "The live room on the publish host is full"
        return RETRY_BACKOFF
