"""The audio of a browser recording (audio/capture.py BrowserProcess, started by
POST /api/audio/start-browser): one WebSocket per source sends 16-bit mono PCM as binary
messages, appended to that source's growing WAV while the recording runs, where live
transcription, the level meters and saving read it as they read pw-record's files.

The browser sends as it records, so a closed laptop loses seconds, not the meeting. A dropped
connection reconnects and carries on in the same file. A new connection for a source becomes
its only writer at once: the old one (often half-dead after a network change) is closed and
writes nothing more, even if it only notices later. Writes never await, so none interleave.
Under /api/ so it needs a token like the rest.
"""

import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ... import access
from ...audio.capture import BrowserProcess
from ..context import get_ws_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audio"])

_current: dict[tuple[str, str], WebSocket] = {}  # each source's one writer


@router.websocket("/api/record/{recording_id}/{source}")
async def record(ws: WebSocket, recording_id: str, source: str):
    ctx = get_ws_ctx(ws)
    found = next(
        ((sid, r) for sid, r in ctx.active_recordings.items() if r.session_id == recording_id),
        None,
    )
    proc = None
    if found is not None:
        proc = next((p for p in found[1].processes if p.source == source), None)
    if proc is None or not isinstance(proc.process, BrowserProcess):
        await ws.close(code=4404)
        return
    owner = ctx.repo.owner_of(found[0])
    if owner is None or not access.can_write(owner):
        await ws.close(code=4403)
        return
    await ws.accept()
    key = (recording_id, source)
    old, _current[key] = _current.get(key), ws
    if old is not None:
        with contextlib.suppress(Exception):
            await old.close(code=4409)  # replaced by this connection
    received = 0
    try:
        with proc.output_path.open("ab") as f:
            while proc.process.returncode is None:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect" or _current.get(key) is not ws:
                    break  # gone, or replaced by a newer connection
                if proc.process.returncode is not None:
                    break  # stopped while waiting: the file is being saved now
                data = message.get("bytes")
                if not data:
                    continue
                data = data[: len(data) - len(data) % 2]  # whole samples only
                f.write(data)
                f.flush()  # readers (live transcript, meters) go by the file's size
                received += len(data)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if _current.get(key) is ws:
            del _current[key]
        if proc.process.returncode is not None:  # stopped: tell the browser it is done
            with contextlib.suppress(Exception):
                await ws.close(code=1000)
    logger.debug("Browser %s audio for %s: %d bytes", source, recording_id, received)
