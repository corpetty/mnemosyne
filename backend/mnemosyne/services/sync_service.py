"""Library sync between a person's computers (docs/plans/2026-10-12-hashseq-collaboration.md,
"Phase 3 design"; what travels and who does what: docs/plans/2026-10-14-library-sync-rules.md).

Each backend PULLS: it asks every peer (services/sync_peers.py) for the meetings changed since
its cursor (`GET /api/sync/changes`, whole documents, a page at a time) and takes each in:

- a meeting deleted here (a tombstone) stays deleted; a tombstone that arrives deletes ours,
  with its audio when it was recorded here (a tombstone beats edits);
- a meeting we lack is written as it is (a fast-forward), and so is one whose version vector
  dominates ours; one ours dominates (or equals) is ignored;
- one edited on both sides (concurrent vectors) is merged three ways (services/sync_merge.py)
  against our state the peer last incorporated: the snapshot we served it at its `vv[us]`;
- a different meeting under the same id (another start time) is a clash: reported, skipped.

Serving (`serve_changes`) remembers each document it hands a peer (`serve_snapshot`), so that
peer's later concurrent edit has a base; a fast-forward taken from a peer is remembered the same
way (both sides then hold that state).

Scheduling: a pull from every peer at start and every `sync_interval` seconds, a pull soon after
a peer says it has news (`poke`, debounced), and a poke to every peer after local changes
(watched through the change counter). Never two rounds with one peer at a time. A peer that does
not answer is tried again after 1, 2, 4... up to 10 minutes; one that pokes or a "Sync now" is
tried at once.

This machine's id in version vectors is its sync id; meetings from before sync counted their
revisions under "" (or none at all) and are CLAIMED at every start: "" becomes the sync id. It
runs always, peers or not: the repository counts new revisions under the sync id from the start,
and a meeting must never carry both keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..models.sync_session import SyncSession, SyncTombstone
from .sync_peers import SyncPairingError, SyncPeer, ensure_dialed, now_iso

if TYPE_CHECKING:
    import httpx

    from ..api.context import AppContext
    from ..models.sync import SyncChanges, SyncContacts

logger = logging.getLogger(__name__)

PROTOCOL = 1
PAGE_SIZE = 50
POKE_DELAY = 2.0  # seconds: pokes within this window make one pull
WATCH_INTERVAL = 3.0  # seconds between looks at the local change counter
BACKOFF_FIRST, BACKOFF_MAX = 60.0, 600.0
# Tombstones are asked for from this long before the last one seen: their times are the
# peer's naive local clock (a DST change or a clock step must not hide one). Re-reading a few
# is harmless: a tombstone we have is skipped.
TOMBSTONE_MARGIN = timedelta(hours=2)
TOMBSTONE_CURSOR = "sync_tombstones_since:"  # + peer id, in the repository's meta table
REQUEST_TIMEOUT = 60.0


class SyncSkew(Exception):
    """The peer speaks another sync protocol."""


class PeerMismatch(Exception):
    """The computer behind the peer's endpoint states another sync id than the one paired."""


class PeerLocked(Exception):
    """The peer's meetings are encrypted and locked (it answered 423)."""


@dataclass
class PeerState:
    """What syncing with one peer is doing, for /api/sync/status (in memory, per start)."""

    state: str = "idle"
    error: str | None = None
    done: int = 0
    total: int = 0
    received: int = 0
    updated: int = 0
    merged: int = 0
    deleted: int = 0
    waiting: int = 0  # meetings left for the next round (their notes merge failed)
    clashes: dict[str, str] = field(default_factory=dict)  # meeting id -> name
    app_version: str | None = None
    warning: str | None = None  # sticky: set and cleared by the round that finds it (contacts)
    backoff: float = 0.0  # seconds; 0 when the peer answered last time
    retry_at: float = 0.0  # time.monotonic() before which the scheduler leaves it alone


@dataclass
class _Outcome:
    kind: str  # new | updated | merged | ignored | clash | deleted | waiting
    session_id: str
    name: str = ""


class SyncService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        # Seconds between pulls from every peer; 0 or None: no background loop (tests drive
        # `sync_round` themselves).
        self.sync_interval: float | None = 30.0
        self.watch_interval = WATCH_INTERVAL
        self.poke_delay = POKE_DELAY
        self.page_size = PAGE_SIZE
        # httpx.AsyncClient for a peer (tests swap in an ASGITransport onto another app).
        self.client_for: Callable[[SyncPeer], httpx.AsyncClient] = self._client_for
        self.states: dict[str, PeerState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._again: set[str] = set()  # a pull was asked for while a round ran
        self._poke_tasks: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None  # made in start(): bound to the running loop
        self._announced = 0  # the change counter last told to the peers
        # Hosted copies of publications that lost a merge (two machines published the same
        # meeting): deleted from the publish host once back on the event loop (flush_host_deletes).
        self.host_deletes: list[dict] = []
        ctx.repo.device_id = self.me

    # ---- identity, claiming -----------------------------------------------------------------

    @property
    def me(self) -> str:
        return self.ctx.sync_peers.self_info().id

    def claim(self) -> int:
        """Version vectors from before sync (the "" key, or none at all) become this machine's.
        No new version: no peer has seen them. Returns how many meetings were claimed."""
        repo, me = self.ctx.repo, self.me
        repo.device_id = me
        changes, _, _ = repo.changes_since(0, limit=10**9)
        claimed = 0
        for sid, _seq, vv in changes:
            if vv and "" not in vv:
                continue
            new = {k: v for k, v in vv.items() if k}
            new[me] = new.get(me, 0) + (vv.get("", 0) or (0 if new else 1))
            state = repo.sync_state(sid)
            if state is None:
                continue
            repo.set_sync_state(sid, new, state[0])
            claimed += 1
        if claimed:
            logger.info(
                "Sync: %d meeting(s) from before sync now count as this computer's", claimed
            )
        return claimed

    # ---- lifetime ---------------------------------------------------------------------------

    def start(self) -> None:
        """At startup (after unlocking): claim old meetings, then pull and watch."""
        self.claim()
        self._announced = self.ctx.repo.current_sync_seq()
        self._locks.clear()
        self._wake = asyncio.Event()
        if self.sync_interval and self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._loop_task, *self._poke_tasks.values(), *self._tasks) if t]
        self._loop_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._poke_tasks.clear()
        self._tasks.clear()

    def wake(self) -> None:
        """Pull from every peer soon (the sidecar just dialed them)."""
        for st in self.states.values():
            st.retry_at = 0.0
        if self._wake is not None:
            self._wake.set()

    async def _run(self) -> None:
        wake = self._wake or asyncio.Event()
        next_pull = 0.0  # at once
        while True:
            try:
                now = time.monotonic()
                if now >= next_pull or wake.is_set():
                    wake.clear()
                    next_pull = now + float(self.sync_interval or 30.0)
                    await self.sync_all()
                await self._announce()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Sync pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), self.watch_interval)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- scheduling -------------------------------------------------------------------------

    def state(self, peer_id: str) -> PeerState:
        return self.states.setdefault(peer_id, PeerState())

    async def sync_all(self, *, force: bool = False) -> None:
        """One round with every peer that is due (all of them with `force`), side by side."""
        if self.ctx.locked:
            return
        now = time.monotonic()
        peers = [
            p for p in self.ctx.sync_peers.peers() if force or self.state(p.id).retry_at <= now
        ]
        if peers:
            await asyncio.gather(*(self.sync_round(p) for p in peers), return_exceptions=True)

    def sync_now(self) -> list[str]:
        """Pull from every peer now, in the background (Sync now). Returns their ids."""
        peers = self.ctx.sync_peers.peers()
        for p in peers:
            self.state(p.id).retry_at = 0.0
            self._spawn(self.sync_round(p))
        return [p.id for p in peers]

    def poke(self, peer_id: str) -> bool:
        """The peer has news: pull from it in `poke_delay` seconds (pokes meanwhile join it)."""
        if self.ctx.sync_peers.get(peer_id) is None:
            return False
        self.state(peer_id).retry_at = 0.0  # it is evidently up
        task = self._poke_tasks.get(peer_id)
        if task is not None and not task.done():
            return True

        async def later():
            await asyncio.sleep(self.poke_delay)
            self._poke_tasks.pop(peer_id, None)
            peer = self.ctx.sync_peers.get(peer_id)
            if peer is not None:
                await self.sync_round(peer)

        self._poke_tasks[peer_id] = asyncio.create_task(later())
        return True

    async def drain(self) -> None:
        """Wait for the pulls pokes and Sync now started (tests)."""
        while pending := [t for t in (*self._poke_tasks.values(), *self._tasks) if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _announce(self) -> None:
        """Local changes since the last look: poke every peer that is reachable."""
        seq = self.ctx.repo.current_sync_seq()
        if seq == self._announced:
            return
        self._announced = seq
        for peer in self.ctx.sync_peers.peers():
            st = self.state(peer.id)
            if st.state in ("offline", "skew") or not peer.listen:
                continue
            try:
                async with self.client_for(peer) as client:
                    await client.post("/api/sync/poke", timeout=10)
            except Exception as e:  # noqa: BLE001 - its next pull from us finds the changes
                logger.debug("Could not poke %s: %s", peer.name, e)

    # ---- one round with one peer ------------------------------------------------------------

    async def sync_round(self, peer: SyncPeer) -> PeerState:
        """Pull everything new from `peer`. Never two at once per peer: asked again while one
        runs, another follows it."""
        if self.ctx.locked:
            return self.state(peer.id)
        lock = self._locks.setdefault(peer.id, asyncio.Lock())
        if lock.locked():
            self._again.add(peer.id)
            return self.state(peer.id)
        async with lock:
            while True:
                self._again.discard(peer.id)
                await self._round(peer)
                if peer.id not in self._again:
                    break
                peer = self.ctx.sync_peers.get(peer.id) or peer
        return self.state(peer.id)

    def _client_for(self, peer: SyncPeer) -> httpx.AsyncClient:
        import httpx

        return httpx.AsyncClient(
            base_url=f"http://{peer.listen}",
            headers={"Authorization": f"Bearer {peer.token}"},
            timeout=REQUEST_TIMEOUT,
        )

    async def _round(self, peer: SyncPeer) -> None:
        import httpx

        from .link import LinkError

        st = self.state(peer.id)
        st.state, st.error, st.done, st.total = "syncing", None, 0, 0
        st.received = st.updated = st.merged = st.deleted = st.waiting = 0
        self._publish(peer, st)
        try:
            if not peer.listen and self.ctx.link.status.running:
                await ensure_dialed(self.ctx.link, self.ctx.sync_peers, peer)
                peer = self.ctx.sync_peers.get(peer.id) or peer
            if not peer.listen:
                raise LinkError("Remote access is not running")
            async with self.client_for(peer) as client:
                await self._hello(client, peer, st)
                await self._contacts(client, peer, st)
                await self._pull(client, peer, st)
            await self._reconcile_tasks(st)
        except SyncSkew as e:
            st.state, st.error = "skew", str(e)
        except PeerMismatch as e:
            st.state, st.error = "error", f"{peer.name} is another computer now: pair again"
            logger.warning("Sync with %s skipped: %s", peer.name, e)
        except PeerLocked:
            st.state, st.error = "locked", f"{peer.name} is locked: enter its recovery code there"
        except (httpx.TransportError, LinkError) as e:
            st.state, st.error = "offline", f"{peer.name} is not reachable"
            st.backoff = min(BACKOFF_MAX, st.backoff * 2) if st.backoff else BACKOFF_FIRST
            st.retry_at = time.monotonic() + st.backoff
            logger.info("Sync with %s: offline (%s), next try in %.0f s", peer.name, e, st.backoff)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            st.state = "error"
            st.error = (
                f"{peer.name} does not accept this computer any more: pair again"
                if code in (401, 403)
                else f"{peer.name} answered {code}"
            )
            logger.warning("Sync with %s: %s", peer.name, st.error)
        except Exception as e:  # noqa: BLE001 - shown in the status; the next round retries
            st.state, st.error = "error", str(e) or type(e).__name__
            logger.exception("Sync with %s failed", peer.name)
        else:
            st.state, st.backoff, st.retry_at = "done", 0.0, 0.0
        self._publish(peer, st)

    async def _reconcile_tasks(self, st: PeerState) -> None:
        """A round that merged or fast-forwarded meetings may have brought a tick on an older
        mention of a linked task: make the mentions agree with their head. Never fails a round."""
        if not (st.updated or st.merged):
            return
        from . import task_links

        try:
            await asyncio.to_thread(task_links.reconcile, self.ctx)
        except Exception:
            logger.warning("Sync: could not reconcile task links", exc_info=True)

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> Any:
        r = await client.get(path, params=params or None)
        if r.status_code == 423:
            raise PeerLocked()
        r.raise_for_status()
        return r.json()

    async def _hello(self, client: httpx.AsyncClient, peer: SyncPeer, st: PeerState) -> None:
        import httpx

        from ..models.sync import SyncHello

        try:
            hello = SyncHello.model_validate(await self._get(client, "/api/sync/hello"))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:  # from before library sync
                raise SyncSkew(f"Update Mnemosyne on {peer.name}") from e
            raise
        st.app_version = hello.app_version
        if hello.protocol > PROTOCOL:
            raise SyncSkew(
                f"{peer.name} runs a newer Mnemosyne ({hello.app_version}): update this computer"
            )
        if hello.protocol < PROTOCOL:
            raise SyncSkew(f"Update Mnemosyne on {peer.name} (it runs {hello.app_version})")
        if hello.id and hello.id != peer.id:
            if not peer.provisional:
                raise PeerMismatch(f"Sync peer {peer.id} calls itself {hello.id} now")
            old = peer.id
            try:
                self.ctx.sync_peers.adopt_id(old, hello.id)
            except SyncPairingError as e:
                raise PeerMismatch(f"Sync peer {old} calls itself {hello.id} now: {e}") from e
            self.ctx.repo.rename_sync_peer(old, hello.id, TOMBSTONE_CURSOR)
            if old in self.states:
                self.states[hello.id] = self.states.pop(old)
            self._locks.pop(old, None)
            logger.info("Sync peer %s is %s", old, hello.id)
        if peer.since > hello.seq:
            # Its change log is younger than our cursor (restored from a backup): start over.
            # Whatever we have already is ignored or merged as usual.
            logger.info(
                "Sync with %s: its change log restarted; reading it from the start", peer.name
            )
            self.ctx.sync_peers.update(peer.id, since=0)

    async def _contacts(self, client: httpx.AsyncClient, peer: SyncPeer, st: PeerState) -> None:
        """Take in the peer's contacts (global to the library, not part of any meeting: they
        never ride in a SyncSession). Merged row by row (`contacts.merge_rows`); the live ones
        that changed get their CONTACT devices and are dialed in the background. An older peer
        answers 404 and a team server 409: nothing to do. A peer under another person key (paired
        before the key was shared) is brought to one key first (`_converge_key`). Only a locked
        or unreachable peer stops the round; any other trouble here is logged and the meetings
        still sync."""
        from ..models.sync import SyncContacts
        from . import contacts

        if self.ctx.settings.team_mode:
            return
        r = await client.get("/api/sync/contacts")
        if r.status_code in (404, 409):
            return
        if r.status_code == 423:
            raise PeerLocked()
        if r.status_code >= 400:
            logger.warning("Sync with %s: its contacts answered %d", peer.name, r.status_code)
            return
        try:
            got = SyncContacts.model_validate(r.json())
        except ValueError:
            logger.warning("Sync with %s: its contacts were not understood", peer.name)
            return
        if got.person != self.ctx.person.public_hex:
            if not await self._converge_key(client, peer, st, got):
                return
        else:
            st.warning = None
        changed = await contacts.apply_peer_rows(self.ctx, [c.model_dump() for c in got.contacts])
        if changed:
            self._spawn(contacts.dial_in_background(self.ctx, changed))

    async def _converge_key(
        self, client: httpx.AsyncClient, peer: SyncPeer, st: PeerState, got: SyncContacts
    ) -> bool:
        """The peer signs with another person key (the two computers were paired before the key
        was shared, so each made its own). Same rule as pairing (`contacts.adoption`), computed on
        both sides from what both know, so they agree without talking: both have people data:
        different people, warn and share no contacts; exactly one has: the other adopts; neither:
        the higher key adopts the lower. Only the adopting side acts (it reads the peer's key and
        takes it); the other changes nothing and finds equal keys next round. True when the keys
        are now equal and the merge can go on."""
        from . import contacts
        from .person import PersonKeys

        mine, theirs = self.ctx.person.public_hex, got.person
        mine_has = contacts.has_people_data(self.ctx)
        if mine_has and got.has_people_data:
            st.warning = f"{peer.name} belongs to another person: contacts are not shared"
            logger.warning("Sync with %s: %s", peer.name, st.warning)
            return False
        if mine_has != got.has_people_data:
            adopt = not mine_has
        else:
            adopt = mine > theirs
        if not adopt:
            return False
        try:
            r = await client.get("/api/sync/person-key")
            r.raise_for_status()
            private = str(r.json()["private"])
            if PersonKeys.public_of(private) != theirs:
                raise ValueError("the key sent is not the one it announced")
            if contacts.has_people_data(self.ctx):  # got contacts since the look above
                raise ValueError("this computer has contacts under its own key")
            self.ctx.person.replace(private)
            contacts.on_key_changed(self.ctx)
        except Exception as e:  # noqa: BLE001 - meetings still sync; the next round tries again
            logger.warning("Sync with %s: could not take its person key: %s", peer.name, e)
            return False
        st.warning = None
        return True

    async def _pull(self, client: httpx.AsyncClient, peer: SyncPeer, st: PeerState) -> None:
        from ..models.sync import SyncChanges

        first = True
        start = pos = peer.since  # the round's first cursor; `pos` is where the next page starts
        held: int | None = None  # the cursor kept for next time when a meeting had to wait
        while True:
            peer = self.ctx.sync_peers.get(peer.id) or peer
            since = pos
            cursor = self._tombstone_cursor(peer.id)
            asked = ""
            if cursor:
                asked = (datetime.fromisoformat(cursor) - TOMBSTONE_MARGIN).isoformat()
            page = SyncChanges.model_validate(
                await self._get(
                    client,
                    "/api/sync/changes",
                    since=since,
                    limit=self.page_size,
                    tombstones_since=asked,
                )
            )
            if first:
                st.total, first = page.total, False
            outcomes, new_cursor, stuck = await asyncio.to_thread(
                self._take_page, peer, page, cursor
            )
            await self.flush_host_deletes()
            for o in outcomes:
                self._count(st, o)
                if o.kind in ("new", "updated", "merged"):
                    s = self.ctx.repo.get(o.session_id)
                    if s is not None:
                        self.ctx.bus.publish(
                            {"type": "session", "session_id": s.id, "status": s.status.value}
                        )
            if new_cursor != cursor and new_cursor:
                self.ctx.repo.set_meta(TOMBSTONE_CURSOR + peer.id, new_cursor)
            if self.ctx.sync_peers.get(peer.id) is None:
                return  # removed meanwhile
            # A meeting that could not be taken in yet (its notes could not be merged) holds the
            # stored cursor back to just before the earliest such meeting of the round, so it is
            # asked for again next round. The round itself goes on to the following pages, so
            # one stuck meeting blocks nothing else. What is taken twice next round is harmless
            # (equal documents make no sidecar call).
            if stuck is not None:
                held = max(start, stuck - 1) if held is None else min(held, max(start, stuck - 1))
            pos = page.next_since
            stored = pos if held is None else min(pos, held)
            self.ctx.sync_peers.update(peer.id, since=stored, last_sync_at=now_iso())
            self._publish(peer, st)
            if not page.more or pos <= since:
                return

    async def flush_host_deletes(self) -> None:
        """Delete the hosted copies queued by `take` (best effort, logged when it fails)."""
        from . import sync_publications

        rows, self.host_deletes = self.host_deletes, []
        for row in rows:
            await sync_publications.delete_hosted(self.ctx, row)

    def _count(self, st: PeerState, o: _Outcome) -> None:
        if o.kind == "clash":
            st.clashes[o.session_id] = o.name
        elif o.kind != "ignored":
            st.clashes.pop(o.session_id, None)
        attr = {
            "new": "received",
            "updated": "updated",
            "merged": "merged",
            "waiting": "waiting",
        }.get(o.kind)
        if o.kind == "deleted":
            attr = "deleted"
        if attr:
            setattr(st, attr, getattr(st, attr) + 1)
        if o.kind != "deleted":
            st.done += 1

    def _tombstone_cursor(self, peer_id: str) -> str:
        return self.ctx.repo.get_meta(TOMBSTONE_CURSOR + peer_id) or ""

    # ---- taking a page in (in a thread: merges run the sidecar) ---------------------------

    def _take_page(
        self, peer: SyncPeer, page: SyncChanges, cursor: str
    ) -> tuple[list[_Outcome], str, int | None]:
        """Every meeting of the page, then its tombstones. Returns what happened, the
        tombstone cursor to keep (not past a tombstone that had to wait) and the change number
        of the earliest meeting that had to wait (None: all were taken). A peer that sends no
        change numbers holds the whole page back."""
        out = [self.take(peer, doc) for doc in page.sessions]
        stuck: int | None = None
        for i, o in enumerate(out):
            if o.kind == "waiting":
                seq = page.seqs[i] if i < len(page.seqs) else 0
                stuck = seq if stuck is None else min(stuck, seq)
        new_cursor, waiting = cursor, False
        for t in sorted(page.tombstones, key=lambda t: (t.at, t.session_id)):
            outcome = self.take_tombstone(peer, t)
            if outcome is None:  # busy here: try again next round
                waiting = True
                continue
            if outcome.kind == "deleted":
                out.append(outcome)
            if not waiting and t.at > new_cursor:
                new_cursor = t.at
        return out, new_cursor, stuck

    def take(self, peer: SyncPeer, theirs: SyncSession) -> _Outcome:
        """One meeting from `peer` into the library. The meeting's notes lock (the one folds and
        catch-ups take) is held throughout: nothing folds the notes document while it is merged."""
        from . import shared_notes, sync_publications

        sid = theirs.id
        if self.ctx.repo.has_tombstone(sid):
            return _Outcome("ignored", sid)
        with shared_notes.hub(self.ctx).lock(sid):
            # What was typed here is folded in first, so our version is current.
            shared_notes.fold_blocking(self.ctx, sid)
            before = self.ctx.repo.session_publications(sid)
            outcome = self._take_locked(peer, theirs)
            if outcome.kind in ("new", "updated", "merged"):
                # Publications that arrived changed: files here that no longer match go.
                try:
                    self.host_deletes += sync_publications.settle(self.ctx, sid, before)
                except Exception:
                    logger.warning("Sync: publication files of %s not settled", sid, exc_info=True)
            return outcome

    def _take_locked(self, peer: SyncPeer, theirs: SyncSession) -> _Outcome:
        from ..models.sync_session import merge_publications
        from . import sync_notes
        from .link import find_binary
        from .shared_notes import NotesDocError
        from .sync_merge import apply, both_docs, merge, vv_relation
        from .sync_state import export_session, same_meeting, write_session

        repo, me, sid = self.ctx.repo, self.me, theirs.id
        ours = export_session(repo, sid, me)
        if ours is None:
            write_session(repo, theirs, local_device_id=me, vv=theirs.vv, bump=False)
            if theirs.notes_doc is not None:
                sync_notes.adopt(self.ctx, sid, theirs.notes_doc, peer.name)
            self._remember(peer, sid)
            return _Outcome("new", sid)
        if not same_meeting(ours.created_at, theirs.created_at):
            logger.warning("Sync: %s on %s is another meeting with this id", sid, peer.name)
            return _Outcome("clash", sid, theirs.name or ours.name)
        relation = vv_relation(ours.vv, theirs.vv)
        if relation in ("equal", "dominates"):
            if theirs.notes_doc is not None and ours.notes_doc is None:
                # We lack the document this meeting has there (an earlier round stopped
                # between the meeting and its document): take it, whatever the versions say.
                sync_notes.adopt(self.ctx, sid, theirs.notes_doc, peer.name)
            return _Outcome("ignored", sid)
        # The documents: a union when both sides have one (stored before the rest of the
        # meeting, with `Session.notes` set to its render), adopted afterwards when only theirs
        # has one. A union that cannot be made leaves the meeting waiting (the cursor stays
        # before it).
        united = None
        if both_docs(ours, theirs):
            try:
                united = sync_notes.union(self.ctx, sid, theirs.notes_doc)
            except NotesDocError as e:
                logger.warning(
                    "Sync: the notes of %s on %s cannot be merged yet, retried next round: %s",
                    sid,
                    peer.name,
                    e,
                )
                return _Outcome("waiting", sid, theirs.name or ours.name)
        adopt_doc = theirs.notes_doc is not None and ours.notes_doc is None
        if relation == "dominated":
            if united is not None:
                sync_notes.store_union(self.ctx, sid, united, peer.name)
                theirs = theirs.model_copy(update={"notes": united.markdown})
            theirs = theirs.model_copy(
                update={"publications": merge_publications(ours.publications, theirs.publications)}
            )
            write_session(repo, theirs, local_device_id=me, vv=theirs.vv, bump=False)
            if adopt_doc:
                sync_notes.adopt(self.ctx, sid, theirs.notes_doc, peer.name)
            self._remember(peer, sid)
            return _Outcome("updated", sid)
        raw = repo.served_snapshot(sid, peer.id, theirs.vv.get(me, 0))
        base = SyncSession.model_validate_json(raw) if raw else None
        if base is None:
            logger.warning("Sync: no base for %s with %s: the newer side is taken", sid, peer.name)
        state = repo.sync_state(sid)
        merged, report = merge(
            base,
            ours,
            theirs,
            ours_is_origin=state is not None and state[0] == "",
            binary=find_binary(),
        )
        if united is not None:
            merged.notes = united.markdown
            sync_notes.store_union(self.ctx, sid, united, peer.name)
            report["notes_document"] = "union"
        apply(
            self.ctx,
            merged,
            local_device_id=me,
            by=f"Sync ({peer.name})",
            report=report,
        )
        if adopt_doc and sync_notes.adopt(self.ctx, sid, theirs.notes_doc, peer.name):
            report["notes_document"] = "adopted"
        return _Outcome("merged", sid)

    def _remember(self, peer: SyncPeer, sid: str) -> None:
        """After a fast-forward both sides hold this state: keep it as a merge base, as if it
        had been served to the peer at our revision in it."""
        from .sync_state import base_json, export_session

        doc = export_session(self.ctx.repo, sid, self.me)
        if doc is not None:
            self.ctx.repo.serve_snapshot(sid, peer.id, doc.vv.get(self.me, 0), base_json(doc))

    def take_tombstone(self, peer: SyncPeer, t: SyncTombstone) -> _Outcome | None:
        """A meeting deleted on a peer: deleted here too (with its audio when it was recorded
        here), and the tombstone kept so it is passed on and never comes back. Its time is ours
        (when we learned of it), so a third computer reading our tombstones from its cursor
        sees it; who deleted it and its name stay. None when the meeting is busy here
        (recording, a job): it waits for the next round."""
        repo, sid = self.ctx.repo, t.session_id
        if repo.has_tombstone(sid):
            return _Outcome("ignored", sid)
        if sid in self.state(peer.id).clashes:  # their meeting under our id, not ours
            return _Outcome("ignored", sid)
        here = repo.sync_state(sid) is not None
        if here and sid in self.ctx.busy_sessions():
            logger.info("Sync: %s was deleted on %s; waiting until it is not busy", sid, peer.name)
            return None
        if here:
            self.ctx.sessions.delete_session(sid)
            logger.info("Sync: deleted %s (deleted on %s)", sid, peer.name)
        repo.add_tombstone(sid, datetime.now().isoformat(), t.device, t.name)
        return _Outcome("deleted" if here else "ignored", sid, t.name)

    # ---- serving ----------------------------------------------------------------------------

    def calling_peer(self, device_id: str | None) -> SyncPeer | None:
        """The sync peer a request came from: its device token's endpoint."""
        if not device_id:
            return None
        device = self.ctx.pairing.get(device_id)
        if device is None or not device.endpoint_id:
            return None
        return self.ctx.sync_peers.find_by_endpoint(device.endpoint_id)

    def serve_changes(
        self, peer: SyncPeer | None, since: int, limit: int, tombstones_since: str
    ) -> SyncChanges:
        """A page of changed meetings for a peer; each document served is remembered as the
        base of that peer's later edits (only when the caller is a known peer)."""
        from ..models.sync import SyncChanges
        from . import shared_notes
        from .sync_state import base_json, export_session

        repo, me = self.ctx.repo, self.me
        limit = max(1, min(limit, 200))
        changes, next_since, more = repo.changes_since(since, limit)
        total = len(repo.changes_since(since, limit=10**9)[0]) if more else len(changes)
        docs, seqs = [], []
        for sid, seq, _vv in changes:
            # The notes document goes out folded (and caught up with text written around it).
            shared_notes.fold_blocking(self.ctx, sid)
            doc = export_session(repo, sid, me)
            if doc is None:
                continue
            if peer is not None:
                repo.serve_snapshot(sid, peer.id, doc.vv.get(me, 0), base_json(doc))
            docs.append(doc)
            seqs.append(seq)
        return SyncChanges(
            sessions=docs,
            seqs=seqs,
            tombstones=[SyncTombstone(**t) for t in repo.tombstones_since(tombstones_since)],
            next_since=next_since,
            more=more,
            total=total,
        )

    # ---- status -----------------------------------------------------------------------------

    def _publish(self, peer: SyncPeer, st: PeerState) -> None:
        self.ctx.bus.publish(
            {
                "type": "sync",
                "peer": peer.id,
                "state": st.state,
                "done": st.done,
                "total": st.total,
            }
        )
