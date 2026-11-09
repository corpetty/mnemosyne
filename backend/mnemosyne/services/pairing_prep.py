"""Making this computer ready for a pairing (design: docs/plans/2026-11-09-connections-design.md).

Every pairing needs an API token (without one anyone who reaches the backend can use it, and a
tunnelled request is never "this machine"), and a computer, library sync or contact also needs
remote access: the link sidecar (services/link.py) that carries the pairing and the traffic
after it. A phone uses neither iroh nor the tunnel, so it needs the token only. The person
never types either: `prepare_pairing` makes them, saves them as the settings route would, starts
the sidecar and waits for it, and says why when that cannot be done.

A token made here is returned to the caller, once. The caller is almost always this computer's
own window, which until now sent no token (it is tokenless while none is set); it has to carry
the new one or its next request is refused (src/lib/api/backend.ts `preparePairing`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import Settings, save_settings
from .link import find_binary

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

# How long to wait for the sidecar's ready line (it reaches its relay first).
PREPARE_TIMEOUT = 10.0


class PairingUnavailable(Exception):
    """Pairing cannot be made ready here; the message says why, for the person."""


@dataclass
class Prepared:
    new_api_token: str | None = None  # the token made now, None when one was already set


async def _persist(ctx: AppContext, **changes) -> None:
    """Save and apply settings the way PUT /api/settings does (api/routes/settings.py)."""
    current = ctx.settings.model_dump()
    current.update(changes)
    new = Settings.model_validate(current)
    save_settings(new)
    await ctx.apply_settings(new)


def _link_up(ctx: AppContext) -> bool:
    status = ctx.link.status
    return bool(status.running and status.ticket)


async def _wait_for_link(ctx: AppContext, timeout: float) -> None:
    """Start the sidecar if it is not going and wait for its ready line. Raises
    PairingUnavailable naming what went wrong."""
    link = ctx.link
    if link._task is not None and link._task.done():  # gave up earlier (no binary): try again
        await link.stop()
    await ctx.apply_remote_access()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not _link_up(ctx):
        gone = link._task is None or link._task.done()
        if gone or loop.time() > deadline:
            reason = link.status.error or (
                "it did not start" if gone else "it did not start in time: try again"
            )
            raise PairingUnavailable(f"Remote access could not be turned on: {reason}")
        await asyncio.sleep(0.02)


async def prepare_pairing(
    ctx: AppContext, *, link: bool = True, timeout: float | None = None
) -> Prepared:
    """Make the API token (and, for `link`, remote access with its sidecar running) exist.

    Idempotent: what is already there is left alone. Everything this call changed is put back
    when the sidecar cannot be started, so a refusal leaves the settings as they were (and no
    token the caller never received). Raises PairingUnavailable. Callers refuse a team server
    for sync and contacts before calling this."""
    st = ctx.settings
    locked = set(st.env_overrides())
    need_token = not st.api_token
    need_access = link and not st.remote_access
    if need_token and "api_token" in locked:
        raise PairingUnavailable("The API token is set by the environment and is empty")
    if need_access and "remote_access" in locked:
        raise PairingUnavailable("Remote access is switched off by the environment")
    if link and not _link_up(ctx) and not (ctx.link.binary or find_binary()):
        raise PairingUnavailable(
            "Remote access needs the mnemosyne-link program, which is not installed here"
        )

    before = {"api_token": st.api_token, "remote_access": st.remote_access}
    token = secrets.token_urlsafe(32) if need_token else None
    changes: dict = {}
    if token:
        changes["api_token"] = token
    if need_access:
        changes["remote_access"] = True
    if changes:
        await _persist(ctx, **changes)
        logger.info("Pairing: turned on %s", " and ".join(sorted(changes)).replace("_", " "))
    if link and not _link_up(ctx):
        try:
            await _wait_for_link(ctx, PREPARE_TIMEOUT if timeout is None else timeout)
        except PairingUnavailable:
            if changes:
                await _persist(ctx, **{k: before[k] for k in changes})
            raise
    return Prepared(new_api_token=token)
