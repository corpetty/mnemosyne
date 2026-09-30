"""WebSocket event stream.

Clients receive every backend event (jobs, sessions, transcription segments), except that on a
firm's server an advisor only hears about their own meetings (access.py): a live transcript is
as private as the meeting. The only client-to-server message is `ping`. Work is started over HTTP.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import access
from .context import AppContext, get_ws_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


def visibility(ctx: AppContext):
    """For this connection's user: may they see an event (or job)? Jobs are theirs when they
    started them or when they are about one of their meetings."""
    only = access.read_owner()
    known: dict[str, bool] = {}

    def meeting(sid: str | None) -> bool:
        if only is None or not sid:
            return True
        if sid not in known:
            # A meeting's owner never changes; a new meeting is looked up on first sight.
            known[sid] = ctx.repo.owner_of(sid) == only
        return known[sid]

    def visible(event: dict) -> bool:
        job = event.get("job")
        if isinstance(job, dict):
            if only is None or job.get("owner_id") == only:
                return True
            return bool(job.get("session_id")) and meeting(job["session_id"])
        return meeting(event.get("session_id"))

    return visible


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    ctx = get_ws_ctx(ws)
    await ws.accept()
    queue = ctx.bus.subscribe()
    visible = visibility(ctx)

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
