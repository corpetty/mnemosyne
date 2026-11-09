"""WebSocket event stream.

Clients receive every backend event (jobs, sessions, transcription segments), except that on a
team server a caller only hears about meetings they may read (the repository's read rule,
access.read_scope): a live transcript is as private as the meeting. Jobs are theirs when they
started them or when they are about a meeting they may read.

On a main computer that also hosts a team (config.hosts_team), events about no meeting (sync
progress, a meeting app starting on the owner's desktop, people sharing) are the owner's own: they
reach only the owner's channels (api/auth.py `is_owner_channel`) unless their type is in
TEAM_EVENTS, which must stay short and carry nothing personal. Default deny: a new session-less
event stays with the owner until it is added there (docs/plans/2026-11-09-team-hosting.md R2).

The only client-to-server message is `ping`. Work is started over HTTP.
"""

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import hosts_team
from .auth import is_owner_channel
from .context import AppContext, get_ws_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


# Session-less events the team's web app may hear on a computer that hosts a team (each
# reviewed: nothing personal in it, and the UI needs it). `job` events are not listed: they have
# their own rule (`visible`).
TEAM_EVENTS = frozenset(
    {
        "hello",  # this connection's own snapshot (built below, filtered item by item)
        "pong",  # the answer to this connection's ping
        "shares",  # a meeting's team shares changed: reload the list (no id, no names)
    }
)


def visibility(ctx: AppContext, state: dict | None = None):
    """For this connection: may it see an event (or job)? `state` is the connection scope's
    state (the channel it came in on, api/auth.py)."""
    state = state if state is not None else {}
    known: dict[str, tuple[bool, float]] = {}  # session id -> (visible, when looked up)

    def meeting(sid: str) -> bool:
        # Sharing changes who may see a meeting during a connection: look again now and then.
        now = time.monotonic()
        seen = known.get(sid)
        if seen is None or now - seen[1] > 10:
            seen = known[sid] = (ctx.repo.exists(sid), now)
        return seen[0]

    def visible(event: dict) -> bool:
        scope = ctx.repo.read_scope()
        job = event.get("job")
        if isinstance(job, dict):
            if scope.everything or job.get("owner_id") == scope.user_id:
                return True
            return bool(job.get("session_id")) and meeting(job["session_id"])
        sid = event.get("session_id")
        if not sid:
            return (
                event.get("type") in TEAM_EVENTS
                or not hosts_team(ctx.settings)
                or is_owner_channel(ctx, state)
            )
        return scope.everything or meeting(sid)

    return visible


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    ctx = get_ws_ctx(ws)
    await ws.accept()
    queue = ctx.bus.subscribe()
    visible = visibility(ctx, ws.scope.get("state"))

    # Snapshot so a reconnecting client can catch up on in-flight work.
    queue.put_nowait(
        {
            "type": "hello",
            "jobs": [
                j.model_dump(mode="json")
                for j in ctx.jobs.list(active_only=True)
                if visible({"job": j.model_dump()})
            ],
            "recovered": [
                r.model_dump(mode="json")
                for r in ctx.recovered
                if visible({"session_id": r.session_id})
            ],
        }
    )

    async def sender():
        while True:
            event = await queue.get()
            if visible(event):
                await ws.send_json(event)

    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "ping":
                queue.put_nowait({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket closed with error", exc_info=True)
    finally:
        send_task.cancel()
        ctx.bus.unsubscribe(queue)
