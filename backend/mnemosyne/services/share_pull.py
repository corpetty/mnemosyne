"""Pull the meetings contacts share with this person (phase 8, task 14; design: "The hub" and
"On the recipient's side" in docs/plans/2026-10-12-hashseq-collaboration.md, C8-C11 in
docs/plans/2026-10-23-phase-8-tasks.md).

For each live contact, a round asks the contact's machines in the order of its card (the
loopback addresses the sidecar dialed, `contacts.dial_all` when none is) for
`GET /api/share/hello` with the token this person presents to them; the first that answers
serves the whole round. Then `GET /api/share/changes` page by page from the cursor kept for
that (contact, machine) in the contact's `cursors` (`{endpoint: {since, tombstones_since}}`):

- a SharedMeeting that does not verify against the contact's key is refused and never stored;
- one whose digest equals the stored copy's is skipped;
- anything else replaces the copy (`repo.apply_shared`, the hub is the truth) and its notes
  document is adopted where we have none, else united with ours (services/sync_notes.py);
- a tombstone deletes our copy when that contact shared it ("removed: <reason> by <name>").

A round runs every `interval` seconds for every contact, at once for one contact on its poke
(`POST /api/share/poke` from the owner, C14) and on "Pull now". Never two rounds per contact at
a time. Pulled meetings are never written through `_touch`, so they never reach library sync:
each of this person's computers is on their card and pulls them itself. Off in team mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from .. import access
from ..models.contact import ContactPullInfo
from ..models.share_hub import SHARE_PROTOCOL, ShareChanges, ShareHello, ShareTombstone
from ..models.shared_meeting import SharedMeeting
from . import contacts

if TYPE_CHECKING:
    import httpx

    from ..api.context import AppContext

logger = logging.getLogger(__name__)

T = TypeVar("T")

INTERVAL = 60.0  # seconds between rounds with every contact when nothing pokes
PAGE_SIZE = 50
REQUEST_TIMEOUT = 60.0
# Tombstones are asked for from this long before the last one seen: their times are the owner's
# naive local clock (a DST change or a clock step must not hide one). One read twice is harmless.
TOMBSTONE_MARGIN = timedelta(hours=2)
# How an owner that removed us is told apart from a token that merely stopped matching (a key
# change, an old pairing): a round in which EVERY machine of the contact answers hello with
# 401/403 counts once; any other round (an answer, or a machine that is unreachable) starts the
# count again. After GONE_AFTER such rounds in a row the contact no longer shares with us: the
# meetings it shared are deleted and the state is "gone". The contact row is kept (the user can
# remove it, or pair again, which brings pulling back). A removed owner usually tells us first
# (it pokes us to pull the tombstones before it drops our token); this is the fallback for an
# owner that was offline then. The count is in memory: it starts again after a restart.
GONE_AFTER = 3


class ShareSkew(Exception):
    """The owner's computer speaks another sharing protocol."""


class _MachineFailed(Exception):
    """One machine of the contact did not serve the round; the next is tried."""

    def __init__(self, kind: str, message: str, every_machine: bool = False):
        super().__init__(message)
        self.kind = kind  # unreachable | refused | old | wrong
        self.every_machine = every_machine  # refused by all of the contact's machines


def describe_failure(e: Exception, name: str) -> tuple[str, str]:
    """(state, message) for what stopped a round or a push with a contact."""
    import httpx

    from .link import LinkError

    if isinstance(e, ShareSkew):
        return "skew", str(e)
    if isinstance(e, _MachineFailed):
        logger.info("Share round with %s: %s", name, e)
        state = {"refused": "error", "wrong": "error", "old": "skew"}.get(e.kind, "unreachable")
        return state, str(e)
    if isinstance(e, (httpx.TransportError, LinkError)):
        logger.info("Share round with %s: not reachable (%s)", name, e)
        return "unreachable", f"{name}'s computers are not reachable"
    if isinstance(e, httpx.HTTPStatusError):
        message = f"{name}'s computer answered {e.response.status_code}"
        logger.warning("Share round with %s: %s", name, message)
        return "error", message
    logger.exception("Share round with %s failed", name, exc_info=e)
    return "error", str(e) or type(e).__name__


@dataclass
class ContactPull:
    """What pulling from one contact did last (in memory, per start)."""

    state: str = "idle"  # idle | pulling | ok | unreachable | skew | error | gone
    at: str | None = None  # when the last round ended, ISO 8601 UTC
    error: str | None = None
    endpoint: str | None = None  # the machine that served the last round
    received: int = 0
    updated: int = 0
    deleted: int = 0
    refused: int = 0
    clashes: dict[str, str] = field(default_factory=dict)  # meeting id -> name

    def info(self) -> ContactPullInfo:
        return ContactPullInfo(state=self.state, at=self.at, error=self.error)


@dataclass
class _Outcome:
    kind: str  # new | parts | updated | ignored | refused | clash | waiting
    session_id: str
    name: str = ""


class SharePuller:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        # Seconds between rounds; 0 or None: no background loop (tests drive the rounds).
        self.interval: float | None = INTERVAL
        self.page_size = PAGE_SIZE
        # httpx.AsyncClient for (loopback address, bearer token); tests swap in a transport.
        self.client_for: Callable[[str, str], httpx.AsyncClient] = self._client_for
        self.states: dict[str, ContactPull] = {}
        self._refused_rounds: dict[str, int] = {}  # consecutive rounds refused by every machine
        self._locks: dict[str, asyncio.Lock] = {}
        self._again: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None

    # ---- lifetime ---------------------------------------------------------------------------

    def start(self) -> None:
        if self.ctx.settings.team_mode:
            return
        self._locks.clear()
        self._wake = asyncio.Event()
        if self.interval and self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._loop_task, *self._tasks) if t]
        self._loop_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def wake(self) -> None:
        """Pull from every contact soon (the sidecar just dialed them)."""
        if self._wake is not None:
            self._wake.set()

    async def _run(self) -> None:
        wake = self._wake or asyncio.Event()
        while True:
            try:
                wake.clear()
                await self.pull_now()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Share pull pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), float(self.interval or INTERVAL))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for the rounds pokes started (tests)."""
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- scheduling -------------------------------------------------------------------------

    def state(self, person: str) -> ContactPull:
        return self.states.setdefault(person, ContactPull())

    def contacts_state(self) -> dict[str, ContactPull]:
        """The pull state of every contact pulled from since this start, by person key."""
        return dict(self.states)

    def info(self, person: str) -> ContactPullInfo | None:
        st = self.states.get(person)
        return st.info() if st is not None else None

    def _available(self) -> bool:
        return not self.ctx.settings.team_mode and not self.ctx.locked

    async def pull_now(self) -> None:
        """One round with every live contact, side by side."""
        if not self._available():
            return
        rows = self.ctx.repo.contacts()
        if rows:
            await asyncio.gather(*(self.pull(r["person"]) for r in rows), return_exceptions=True)

    def poke(self, person: str) -> bool:
        """The contact has news: pull from it now, in the background. False for no live
        contact (or sharing off here)."""
        if not self._available():
            return False
        row = self.ctx.repo.contact(person)
        if row is None or row["removed_at"]:
            return False
        self._spawn(self.pull(person))
        return True

    async def pull(self, person: str) -> ContactPull | None:
        """A round with one contact now; None for no live contact. Asked again while one runs,
        another follows it (and this returns the running one's state)."""
        if not self._available():
            return None
        row = self.ctx.repo.contact(person)
        if row is None or row["removed_at"]:
            return None
        lock = self._locks.setdefault(person, asyncio.Lock())
        if lock.locked():
            self._again.add(person)
            return self.state(person)
        # The round is this computer's own work, whoever's request (a poke) started it.
        reset = access.current.set(None)
        try:
            async with lock:
                while True:
                    self._again.discard(person)
                    await self._round(person)
                    if person not in self._again:
                        break
        finally:
            access.current.reset(reset)
        return self.state(person)

    # ---- one round with one contact ---------------------------------------------------------

    def _client_for(self, listen: str, token: str) -> httpx.AsyncClient:
        import httpx

        return httpx.AsyncClient(
            base_url=f"http://{listen}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )

    async def _round(self, person: str) -> None:
        row = self.ctx.repo.contact(person)
        if row is None or row["removed_at"]:
            return
        name = contacts.my_or_card_name(row) or "This contact"
        st = self.state(person)
        st.state, st.error = "pulling", None
        st.received = st.updated = st.deleted = st.refused = 0
        self._publish(person, st)
        try:
            await self._push_first(row)
            await self._pull_from(row, st)
        except Exception as e:  # noqa: BLE001 - shown in the status; the next round retries
            st.state, st.error = describe_failure(e, name)
            refused = isinstance(e, _MachineFailed) and e.kind == "refused" and e.every_machine
            self._count_refusal(row, st, name, refused)
        else:
            self._refused_rounds.pop(person, None)
            st.state = "ok"
        st.at = datetime.now(UTC).isoformat()
        self._publish(person, st)

    def _count_refusal(self, row: dict, st: ContactPull, name: str, refused: bool) -> None:
        """See GONE_AFTER."""
        person = row["person"]
        if not refused:
            self._refused_rounds.pop(person, None)
            return
        n = self._refused_rounds[person] = self._refused_rounds.get(person, 0) + 1
        if n < GONE_AFTER:
            return
        st.state, st.error = "gone", f"{name} no longer shares with you"
        if n == GONE_AFTER:
            contacts.forget_their_meetings(self.ctx, row, f"{name} no longer shares with you")

    async def _machines(self, row: dict) -> list[tuple[str, str]]:
        """(endpoint id, loopback address) of the contact's machines in the card's order, those
        dialed; when none is, they are dialed first."""
        order = contacts.endpoints(row["card"])
        machines = row.get("machines") or {}

        def dialed(m: dict) -> list[tuple[str, str]]:
            return [
                (ep, str(m[ep]["listen"]))
                for ep in order
                if isinstance(m.get(ep), dict) and m[ep].get("listen")
            ]

        found = dialed(machines)
        if not found and self.ctx.link.status.running:
            try:
                found = dialed(await contacts.dial_all(self.ctx, row))
            except Exception as e:  # noqa: BLE001 - reported as unreachable
                logger.info("Could not dial %s's computers: %s", row.get("name"), e)
        return found

    async def _push_first(self, row: dict) -> None:
        """What I did on this contact's meetings goes first, so the copy pulled next already has
        it. Never fails the round: what could not be sent stays in the outbox."""
        from . import share_push

        try:
            await share_push.push(self.ctx, row)
        except Exception:  # noqa: BLE001
            logger.exception("Share push to %s failed", contacts.my_or_card_name(row))

    async def _pull_from(self, row: dict, st: ContactPull) -> None:
        async def serve(client: httpx.AsyncClient, endpoint: str) -> None:
            st.endpoint = endpoint
            await self._pages(client, row, endpoint, st)

        await self.with_machine(row, serve)

    async def with_machine(
        self, row: dict, fn: Callable[[httpx.AsyncClient, str], Awaitable[T]]
    ) -> T:
        """Run `fn(client, endpoint id)` against the first of the contact's machines that
        answers `hello` as them, with the token this person presents to them. Raises
        _MachineFailed (nothing reachable, or what they said about us), ShareSkew, or what `fn`
        raises (httpx errors included)."""
        import httpx

        from .link import LinkError

        person = row["person"]
        name = contacts.my_or_card_name(row) or "This contact"
        machines = await self._machines(row)
        if not machines:
            raise _MachineFailed("unreachable", f"{name}'s computers are not reachable")
        token, _ = contacts.tokens(row["secret"], self.ctx.person.public_hex, person)
        failures: list[_MachineFailed] = []
        for endpoint, listen in machines:
            async with self.client_for(listen, token) as client:
                try:
                    await self._hello(client, row)
                except _MachineFailed as e:
                    failures.append(e)
                    continue
                except (httpx.TransportError, LinkError) as e:
                    failures.append(_MachineFailed("unreachable", str(e)))
                    continue
                return await fn(client, endpoint)
        for kind, message in (
            ("refused", f"{name}'s computer does not accept this one any more: pair again"),
            ("wrong", f"{name}'s computer answers as someone else: pair again"),
            ("old", f"{name}'s computer runs an older Mnemosyne"),
        ):
            if any(f.kind == kind for f in failures):
                raise _MachineFailed(
                    kind, message, every_machine=all(f.kind == kind for f in failures)
                )
        raise _MachineFailed("unreachable", f"{name}'s computers are not reachable")

    async def _hello(self, client: httpx.AsyncClient, row: dict) -> ShareHello:
        import httpx

        name = contacts.my_or_card_name(row) or "This contact"
        r = await client.get("/api/share/hello")
        if r.status_code in (401, 403):
            raise _MachineFailed("refused", f"answered {r.status_code}")
        if r.status_code in (404, 405):
            raise _MachineFailed("old", f"answered {r.status_code}")
        try:
            r.raise_for_status()
            hello = ShareHello.model_validate(r.json())
        except (httpx.HTTPStatusError, ValueError) as e:
            raise _MachineFailed("unreachable", str(e)) from e
        if hello.person != row["person"]:
            raise _MachineFailed("wrong", f"it is {hello.person[:16]} now")
        if hello.protocol > SHARE_PROTOCOL:
            raise ShareSkew(f"Update Mnemosyne to see what {name} shares")
        if hello.protocol < SHARE_PROTOCOL:
            raise ShareSkew(f"{name} needs a newer Mnemosyne (it runs {hello.app_version})")
        return hello

    async def _pages(
        self, client: httpx.AsyncClient, row: dict, endpoint: str, st: ContactPull
    ) -> None:
        person = row["person"]
        held = (row.get("cursors") or {}).get(endpoint) or {}
        since = int(held.get("since") or 0)
        tombs = str(held.get("tombstones_since") or "")
        stuck = False  # a meeting had to wait: the stored cursor stays before it
        while True:
            asked = ""
            if tombs:
                try:
                    asked = (datetime.fromisoformat(tombs) - TOMBSTONE_MARGIN).isoformat()
                except ValueError:
                    asked = ""
            r = await client.get(
                "/api/share/changes",
                params={"since": since, "tombstones_since": asked, "limit": self.page_size},
            )
            r.raise_for_status()
            page = ShareChanges.model_validate(r.json())
            outcomes = await asyncio.to_thread(self._take_page, row, page.sessions)
            outcomes += self._take_tombstones(row, page.tombstones)
            self._after(row, st, outcomes)
            if any(o.kind == "waiting" for o in outcomes):
                stuck = True
            if not stuck:
                fresh = self.ctx.repo.contact(person)
                if fresh is None:
                    return
                cursors = dict(fresh.get("cursors") or {})
                cursors[endpoint] = {
                    "since": page.next_since,
                    "tombstones_since": max(tombs, page.tombstones_next),
                }
                self.ctx.repo.set_contact_runtime(person, cursors=cursors)
            tombs = max(tombs, page.tombstones_next)
            if not page.more or page.next_since <= since:
                return
            since = page.next_since

    # ---- taking a page in -------------------------------------------------------------------

    def _take_page(self, row: dict, sessions: list[SharedMeeting]) -> list[_Outcome]:
        """In a thread: notes merges run the sidecar."""
        with access.internally():
            return [self.take(row, sm) for sm in sessions]

    def take(self, row: dict, sm: SharedMeeting) -> _Outcome:
        """One meeting the contact shares. Blocking. The meeting's notes lock is held while its
        document is merged, as library sync does."""
        from . import share_push, shared_notes, sync_notes
        from .shared_notes import NotesDocError
        from .shares import verify_shared_meeting

        repo, person = self.ctx.repo, row["person"]
        name = contacts.my_or_card_name(row)
        sid = sm.id
        if not verify_shared_meeting(sm, person):
            logger.warning("Refused meeting %s from %s: its signature does not verify", sid, name)
            return _Outcome("refused", sid, sm.name)
        held = repo.shared_meeting(sid)
        if held is not None and held["person"] == person and held["digest"] == sm.digest:
            return _Outcome("ignored", sid)
        here = repo.notes_source(sid) is not None
        if here and repo.shared_from(sid) != person:
            logger.warning(
                "Shared meeting %s from %s has the id of another meeting here", sid, name
            )
            return _Outcome("clash", sid, sm.name)
        with shared_notes.hub(self.ctx).lock(sid):
            united = None
            doc = sm.notes_doc if sm.parts.notes else ""
            ours = here and sync_notes.local_snapshot(self.ctx, sid) is not None
            if doc and ours:
                shared_notes.fold_blocking(self.ctx, sid)  # what was typed here first
                try:
                    united = sync_notes.union(self.ctx, sid, doc)
                except NotesDocError as e:
                    logger.warning(
                        "The notes of %s from %s cannot be merged yet, retried: %s", sid, name, e
                    )
                    return _Outcome("waiting", sid, sm.name)
            if united is not None:
                sync_notes.store_union(self.ctx, sid, united, name)
                kind = repo.apply_shared(sm.model_copy(update={"notes": united.markdown}), person)
            else:
                kind = repo.apply_shared(sm, person)
            if kind == "clash":
                return _Outcome("clash", sid, sm.name)
            if doc and not ours:
                sync_notes.adopt(self.ctx, sid, doc, name)
            # The copy is the owner's whole: what I did and they have not accepted yet is
            # shown again on top of it (services/share_push.py).
            share_push.reapply_pending(self.ctx, sid)
        return _Outcome(kind, sid, sm.name)

    def _take_tombstones(self, row: dict, tombstones: list[ShareTombstone]) -> list[_Outcome]:
        """On the event loop (a delete is quick): forget the copies this contact withdrew.
        Only a copy that contact shared is ever deleted."""
        out = []
        name = contacts.my_or_card_name(row)
        for t in tombstones:
            sid = t.session_id
            if self.ctx.repo.shared_from(sid) != row["person"]:
                continue
            with access.internally():
                deleted = self.ctx.sessions.delete_session(sid)
            if deleted:
                logger.info("Shared meeting %s removed: %s by %s", sid, t.reason, name)
                out.append(_Outcome("deleted", sid))
        return out

    def _after(self, row: dict, st: ContactPull, outcomes: list[_Outcome]) -> None:
        """On the event loop: counts, history lines, and the events that refresh the UI."""
        from . import history

        name = contacts.my_or_card_name(row)
        for o in outcomes:
            if o.kind == "clash":
                st.clashes[o.session_id] = o.name
            elif o.kind != "ignored":
                st.clashes.pop(o.session_id, None)
            if o.kind == "new":
                st.received += 1
                history.log(self.ctx, o.session_id, "shared_with_me", by=name)
            elif o.kind == "parts":
                st.updated += 1
                held = self.ctx.repo.shared_meeting(o.session_id) or {}
                chosen = [k for k, v in (held.get("parts") or {}).items() if v is True]
                history.log(self.ctx, o.session_id, "share_updated", by=name, parts=chosen)
            elif o.kind == "updated":
                st.updated += 1
            elif o.kind == "deleted":
                st.deleted += 1
            elif o.kind == "refused":
                st.refused += 1
            if o.kind in ("new", "parts", "updated"):
                self.ctx.bus.publish(
                    {"type": "session", "session_id": o.session_id, "status": "completed"}
                )

    def _publish(self, person: str, st: ContactPull) -> None:
        self.ctx.bus.publish(
            {"type": "share_pull", "person": person, "state": st.state, "error": st.error}
        )
