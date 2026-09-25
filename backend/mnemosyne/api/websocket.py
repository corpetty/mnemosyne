"""WebSocket event stream.

Clients receive every backend event (jobs, sessions, transcription segments).
The only client-to-server message is `ping`. Work is started over HTTP.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .context import get_ws_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    ctx = get_ws_ctx(ws)
    await ws.accept()
    queue = ctx.bus.subscribe()

    # Snapshot so a reconnecting client can catch up on in-flight work.
    queue.put_nowait(
        {
            "type": "hello",
            "jobs": [j.model_dump(mode="json") for j in ctx.jobs.list(active_only=True)],
            "recovered": [r.model_dump(mode="json") for r in ctx.recovered],
        }
    )

    async def sender():
        while True:
            event = await queue.get()
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
