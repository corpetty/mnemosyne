"""WebSocket endpoint for real-time transcription streaming."""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.model_service import model_service
from ..services.session_service import session_service

logger = logging.getLogger(__name__)

router = APIRouter()


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, message: dict):
        """Send a message to all connected clients."""
        text = json.dumps(message)
        for ws in self.active:
            try:
                await ws.send_text(text)
            except Exception:
                pass


manager = ConnectionManager()


async def broadcast_transcription(audio_path: str, session_id: str | None = None):
    """Run transcription and broadcast segments to all connected clients.

    If session_id is provided, saves transcript to the session on completion.
    """
    try:
        await manager.broadcast({
            "type": "status",
            "session_id": session_id,
            "message": "Loading models...",
        })

        engine = await model_service.ensure_loaded()

        await manager.broadcast({
            "type": "status",
            "session_id": session_id,
            "message": "Transcribing...",
        })

        segments = []
        async for segment in engine.transcribe(audio_path):
            seg_dict = segment.model_dump()
            segments.append(seg_dict)
            await manager.broadcast({
                "type": "transcription",
                "session_id": session_id,
                "segment": seg_dict,
            })

        # Save to session if we have one
        if session_id and segments:
            session_service.set_transcript(session_id, segments)

        await manager.broadcast({
            "type": "status",
            "session_id": session_id,
            "message": "Transcription complete",
        })

        return segments

    except Exception as e:
        logger.exception("Transcription failed")
        await manager.broadcast({
            "type": "error",
            "session_id": session_id,
            "message": str(e),
        })
        return []


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "transcribe":
                audio_path = msg.get("audio_path", "")
                sid = msg.get("session_id")
                if audio_path:
                    await broadcast_transcription(audio_path, session_id=sid)

            elif msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        manager.disconnect(ws)
