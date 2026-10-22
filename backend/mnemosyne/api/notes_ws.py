"""WebSocket of a meeting's live shared notes: `/ws/notes/{session_id}`.

Auth as /ws (api/auth.py: a token in `?token=` on a team server or with an API token). A meeting
the caller may not read (or none) is closed with 4404. Binary messages are hashseq frames both
ways, relayed as they are (services/shared_notes.py); JSON text messages carry presence. The
protocol is in docs/api-reference.md ("Live notes").
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import access
from ..services import shared_notes
from .context import AppContext, get_ws_ctx

logger = logging.getLogger(__name__)

router = APIRouter()


def identity(ctx: AppContext) -> tuple[str, str]:
    """Who is typing: the team user, or on the desktop its `local_speaker_name`."""
    p = access.principal()
    if p is not None:
        return p.user_id, p.name
    return "", (ctx.settings.local_speaker_name or "").strip() or "You"


@router.websocket("/ws/notes/{session_id}")
async def notes_socket(ws: WebSocket, session_id: str):
    ctx = get_ws_ctx(ws)
    await ws.accept()
    if ctx.locked or not ctx.repo.exists(session_id):  # Hidden and unknown alike
        await ws.close(code=4404)
        return
    you_id, you_name = identity(ctx)
    can_edit = shared_notes.may_edit(ctx, session_id)
    member = await shared_notes.join(ctx, session_id, ws, you_id, you_name, can_edit)
    if member is None:
        await ws.send_json({"type": "hello", "unavailable": True})
        await ws.close()
        return
    close_code = None
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                close_code = shared_notes.receive_frame(ctx, session_id, member, msg["bytes"])
                if close_code is not None:
                    break
            elif msg.get("text") is not None:
                shared_notes.receive_text(ctx, session_id, member, msg["text"])
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Notes socket closed with error", exc_info=True)
    finally:
        await shared_notes.leave(ctx, session_id, member, close_code)
