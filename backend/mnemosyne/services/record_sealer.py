"""The record sealer (docs/plans/2026-11-09-records-by-frontier.md, 3.5 and decision D2): seals a
meeting by itself after the app changed it, so that the wider coverage of a format 2 seal
(notes, comments, details, ...) does not turn every comment or notes edit into a red "changed
outside the app" until the next transcription.

It polls the library-sync change log (`repo.seal_candidates`, the counter every travelling write
already moves), keeps the meetings that changed, and once one has been quiet for `QUIET` seconds
(or has waited `MAX_WAIT` seconds since its first unsealed change, so people typing together for
minutes still get sealed) and is not busy, compares it with its last seal part by part. Only a
difference seals: the reason lists what differs ("changed: notes, comments"). A change that no
part sees (the derived state `_touch` also counts) seals nothing.

Left alone: a meeting being recorded or with a job running (the job's own seals follow, and the
next look finds it quiet), a meeting shared with us (`shared_from`, `seal_candidates` leaves it
out), a locked library. The cursor (meta `records_sealer_seq`) is the last change number looked
at, held back to just before the oldest meeting still waiting, so a change made while the
backend was down is caught up at the next start. The first start sets it to the current number:
an upgrade does not seal the library at once (D5).

The laundering window of D2: a row forged in the database and followed within minutes by any
app change to the same meeting is sealed as if the app wrote it. The chain head written down
elsewhere (exports, backups) is the real defence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..storage.records import compare_audio, compare_parts, manifest_for

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

WATCH_INTERVAL = 5.0  # seconds between looks at the change counter (0: no loop, tests tick)
QUIET = 60.0  # a change counts once the meeting has been quiet this long
MAX_WAIT = 600.0  # ... or once its first unsealed change is this old, quiet or not
BACKOFF = (60.0, 120.0, 300.0, 600.0)  # after the 1st, 2nd, 3rd, 4th and later failures
PAGE = 200  # candidates read from the log at a time
CURSOR_KEY = "records_sealer_seq"


def changed_reason(parts: list[str]) -> str:
    """The seal's reason: what differs from the last seal."""
    return "changed: " + ", ".join(parts) if parts else "changed"


class RecordSealer:
    """`tick()` is one look and is what tests drive (with `clock` swapped and `interval` 0);
    `start()` runs it every `interval` seconds."""

    def __init__(self, ctx: AppContext, clock: Callable[[], float] = time.monotonic):
        self.ctx = ctx
        self.clock = clock
        self.interval = WATCH_INTERVAL
        self.quiet = QUIET
        self.max_wait = MAX_WAIT
        self.backoff = BACKOFF
        self._seen = 0  # the change number last looked at
        self._dirty: dict[str, float] = {}  # session id -> clock time of its last change
        self._first: dict[str, float] = {}  # session id -> its first change not yet sealed
        self._since: dict[str, int] = {}  # session id -> change number it first became dirty at
        self._failures: dict[str, tuple[int, float]] = {}  # session id -> (count, retry_at)
        self._task: asyncio.Task | None = None

    # ---- lifetime ---------------------------------------------------------------------------

    def start(self) -> None:
        """At startup (after unlocking): meetings that changed since the stored cursor are
        looked at (their quiet time starts now), then the change log is watched."""
        repo = self.ctx.repo
        stored = repo.get_meta(CURSOR_KEY)
        if stored is None:  # first start: what is already there is not sealed in one go
            self._seen = repo.current_sync_seq()
            repo.set_meta(CURSOR_KEY, str(self._seen))
        else:
            self._seen = int(stored)
        self._read_changes(self.clock())
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

    # ---- one look ---------------------------------------------------------------------------

    async def tick(self) -> list[str]:
        """Look once; returns the reasons of the seals written (in order). Never raises."""
        try:
            if self.ctx.locked:
                return []
            return await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next look tries again
            logger.warning("Record sealer: the pass failed", exc_info=True)
            return []

    async def _tick(self) -> list[str]:
        now = self.clock()
        self._read_changes(now)
        sealed: list[str] = []
        busy = self.ctx.busy_sessions() if self._dirty else set()
        for sid, changed in list(self._dirty.items()):
            if now - changed < self.quiet and now - self._first.get(sid, changed) < self.max_wait:
                continue
            if sid in busy:
                continue  # looked at again once the recording or job is over
            failure = self._failures.get(sid)
            if failure is not None and now < failure[1]:
                continue
            self._dirty.pop(sid, None)
            self._first.pop(sid, None)
            self._since.pop(sid, None)
            outcome = await asyncio.to_thread(self.seal_if_changed, sid)
            if outcome is None:
                self._failures.pop(sid, None)
            elif outcome == "":  # could not be sealed: try again later
                count = (failure[0] if failure else 0) + 1
                delay = self.backoff[min(count, len(self.backoff)) - 1]
                self._failures[sid] = (count, self.clock() + delay)
                self._dirty[sid] = now
                self._first[sid] = now
                self._since[sid] = self._seen
            else:
                self._failures.pop(sid, None)
                sealed.append(outcome)
        self._save_cursor()
        return sealed

    def _read_changes(self, now: float) -> None:
        """Meetings changed since the last look become dirty (their quiet time starts again)."""
        repo = self.ctx.repo
        after = self._seen
        while True:
            found = repo.seal_candidates(after, PAGE)
            for sid, seq in found:
                self._dirty[sid] = now
                self._first.setdefault(sid, now)
                self._since.setdefault(sid, seq)
                after = max(after, seq)
            if len(found) < PAGE:
                break
        self._seen = max(self._seen, after)

    def _save_cursor(self) -> None:
        """The cursor stays just before the oldest meeting still waiting."""
        cursor = min(self._since.values()) - 1 if self._since else self._seen
        try:
            if str(cursor) != self.ctx.repo.get_meta(CURSOR_KEY):
                self.ctx.repo.set_meta(CURSOR_KEY, str(cursor))
        except Exception:  # noqa: BLE001 - only costs a repeated look after a restart
            logger.warning("Record sealer: could not store its cursor", exc_info=True)

    # ---- one meeting ------------------------------------------------------------------------

    def seal_if_changed(self, session_id: str) -> str | None:
        """Blocking (hashes audio; run in a thread). Seals the meeting when a part or an audio
        file differs from its last seal (or when the last seal is not in the current format).
        Returns the reason of the seal written, None when there was nothing to seal (no
        difference, nothing worth keeping, the meeting is gone or shared with us), and "" when
        it should have been sealed and was not (an audio file that cannot be read)."""
        from . import records

        app = self.ctx
        session = app.repo.get(session_id)
        if session is None or session.shared_from:
            return None
        comments = app.repo.list_comments(session_id)
        if not records._sealable(session, comments):
            return None
        seals = app.repo.seals(session_id)
        last = seals[-1] if seals else None
        parts: list[str] = []
        if last is not None and last["format"] == 2 and last["manifest"]:
            sealed = last["manifest"]
            now = manifest_for(session, comments, rev=sealed["rev"], audio_here=False)
            parts = [d.name for d in compare_parts(sealed["parts"], now["parts"])]
            audio, _, failed = records._audio_now(app, session, last)
            if failed:
                return ""  # `seal` would not write it either
            if sealed["audio_here"] != (not session.origin) or compare_audio(
                sealed["audio"], sealed["erased"], audio
            ):
                parts.append("audio")
            if not parts:
                return None
        reason = changed_reason(parts)
        written = records.seal(app, session_id, reason)
        return reason if written is not None else ""
