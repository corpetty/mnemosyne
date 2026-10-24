"""Tell a contact that a meeting shared with them changed (phase 8, task 13).

Watches the repo's change counter like library sync's announcer (services/sync_service.py
`_announce`); when it rose, finds the meetings that changed, the contacts they are shared with
(a share revoked since the last look counts once, so the contact learns of the revoke) and
POSTs `/api/share/poke` to each of that contact's dialed machines. A poke only says "pull now":
the contact's own pull finds the changes. Never raises; off in team mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from ..api.context import AppContext

logger = logging.getLogger(__name__)

WATCH_INTERVAL = 3.0  # seconds between looks at the change counter
DEBOUNCE = 3.0  # at most one poke per contact in this many seconds
POKE_TIMEOUT = 10.0
PAGE = 500


class ShareAnnouncer:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.watch_interval = WATCH_INTERVAL
        self.debounce = DEBOUNCE
        # httpx.AsyncClient for (loopback address, bearer token); tests swap in a transport.
        self.client_for: Callable[[str, str], httpx.AsyncClient] = self._client_for
        self._seen = 0  # the change counter last looked at
        self._looked_at = ""  # ISO time of the last look: revocations after it are news
        self._last_poke: dict[str, float] = {}  # person -> monotonic time of the last poke
        self._pending: set[str] = set()  # contacts to poke once their debounce is over
        self._task: asyncio.Task | None = None

    # ---- lifetime ---------------------------------------------------------------------------

    def start(self) -> None:
        if self.ctx.settings.team_mode:
            return
        self._seen = self.ctx.repo.current_sync_seq()
        self._looked_at = datetime.now().isoformat()
        if self.watch_interval and self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.info("Share announcer pass failed", exc_info=True)
            await asyncio.sleep(self.watch_interval)

    # ---- one look ---------------------------------------------------------------------------

    async def tick(self) -> list[str]:
        """Look once; returns the contacts (person keys) poked. Never raises."""
        try:
            if self.ctx.settings.team_mode or self.ctx.locked:
                return []
            return await self._tick()
        except Exception as e:  # noqa: BLE001 - the contact's next pull finds the changes
            logger.info("Could not announce shared meetings: %s", e)
            return []

    async def _tick(self) -> list[str]:
        repo = self.ctx.repo
        looked = datetime.now().isoformat()
        seq = repo.current_sync_seq()
        if seq != self._seen:
            since, self._seen = self._seen, seq
            before, self._looked_at = self._looked_at, looked
            self._pending |= self._affected(since, before)
        if not self._pending:
            return []
        now = time.monotonic()
        due = [
            p for p in sorted(self._pending) if now - self._last_poke.get(p, -1e9) >= self.debounce
        ]
        poked = []
        for person in due:
            self._pending.discard(person)
            self._last_poke[person] = now
            if await self._poke(person):
                poked.append(person)
        return poked

    def _affected(self, since_seq: int, since_iso: str) -> set[str]:
        """Contacts with a share on a meeting that changed after `since_seq`, or whose share
        was revoked after `since_iso`."""
        repo = self.ctx.repo
        people: set[str] = set()
        after = since_seq
        while True:
            changes, after, more = repo.changes_since(after, PAGE)
            for sid, _seq, _vv in changes:
                for share in repo.peer_shares(sid, include_revoked=True):
                    if not share["revoked_at"] or share["revoked_at"] > since_iso:
                        people.add(share["person"])
            if not more:
                return people

    async def _poke(self, person: str) -> bool:
        from . import contacts

        ctx = self.ctx
        row = ctx.repo.contact(person)
        if row is None or row.get("removed_at"):
            return False
        try:
            presented, _ = contacts.tokens(row["secret"], ctx.person.public_hex, person)
            machines = row.get("machines") or {}
            if not any(m.get("listen") for m in machines.values()):
                machines = await contacts.dial_all(ctx, row)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not reach %s to poke: %s", row.get("name"), e)
            return False
        ok = False
        for endpoint, machine in machines.items():
            listen = machine.get("listen")
            if not listen:
                continue
            try:
                async with self.client_for(listen, presented) as client:
                    await client.post("/api/share/poke", timeout=POKE_TIMEOUT)
                ok = True
            except Exception as e:  # noqa: BLE001 - its next pull finds the changes
                logger.debug("Could not poke %s (%s): %s", row.get("name"), endpoint[:8], e)
        return ok

    @staticmethod
    def _client_for(listen: str, token: str) -> httpx.AsyncClient:
        import httpx

        return httpx.AsyncClient(
            base_url=f"http://{listen}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=POKE_TIMEOUT,
        )
