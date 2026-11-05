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

The bridge to the host's room (task 6) comes later; until then `status()` reports "offline".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import access

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
    `live_reason` and `readers`: state is "live", "offline" or "unsupported". Only called for a
    row with `follow` or `comments` on. Until the bridge exists nothing is connected; the
    reason is the watcher's last failure for the meeting, if any."""
    watcher = getattr(app, "live_watcher", None)
    reason = watcher.reason(row["session_id"]) if watcher is not None else ""
    return ("offline", reason, 0)


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
