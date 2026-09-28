"""Share a quote from a transcript: its text, and an audio clip of it."""

import asyncio
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ...models.base import ApiModel
from ...services.clips import clip_filename, cut_clip, quote_text
from ...storage.crypto import EncryptedFile, is_encrypted
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions", tags=["clips"])

CLIP_ID = re.compile(r"^\d+-\d+$")


class ClipRequest(ApiModel):
    first_idx: int
    last_idx: int
    audio: bool = True  # also cut the audio (text only when false or there is no audio)


class Clip(ApiModel):
    id: str
    filename: str  # suggested name when saving
    seconds: float


class Quote(ApiModel):
    text: str  # markdown blockquote with speakers and times
    clip: Clip | None


class SaveClipRequest(ApiModel):
    path: str  # where to copy it, on this computer


def _clips_dir(ctx: AppContext, session_id: str) -> Path:
    return ctx.settings.recordings_dir / session_id / "clips"


@router.post("/{session_id}/clip", response_model=Quote)
async def make_quote(session_id: str, request: ClipRequest, ctx: AppContext = Depends(get_ctx)):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    first, last = request.first_idx, request.last_idx
    if not (0 <= first <= last < len(session.transcript)):
        raise HTTPException(status_code=400, detail="Those lines are not in the transcript")
    text = quote_text(session, first, last)
    clip = None
    if request.audio and session.audio_file:
        try:
            await cut_clip(session, _clips_dir(ctx, session_id), first, last, ctx.file_key)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        start, end = session.transcript[first].start, session.transcript[last].end
        clip = Clip(
            id=f"{first}-{last}",
            filename=clip_filename(session, start),
            seconds=round(end - start, 1),
        )
    return Quote(text=text, clip=clip)


def _clip_path(ctx: AppContext, session_id: str, clip_id: str) -> Path:
    if not CLIP_ID.match(clip_id):
        raise HTTPException(status_code=404, detail="Clip not found")
    for name in (f"{clip_id}.ogg", f"{clip_id}.ogg.enc"):
        path = _clips_dir(ctx, session_id) / name
        if path.exists():
            return path
    raise HTTPException(status_code=404, detail="Clip not found")


@router.get("/{session_id}/clips/{clip_id}")
async def get_clip(session_id: str, clip_id: str, ctx: AppContext = Depends(get_ctx)):
    path = _clip_path(ctx, session_id, clip_id)
    session = ctx.sessions.get_session(session_id)
    first = int(clip_id.split("-")[0])
    if session is None or first >= len(session.transcript):
        name = path.name
    else:
        name = clip_filename(session, session.transcript[first].start)
    if is_encrypted(path):
        if ctx.file_key is None:
            raise HTTPException(status_code=423, detail="Meetings are encrypted and locked")
        data = await asyncio.to_thread(EncryptedFile(path, ctx.file_key).read)
        return Response(data, media_type="audio/ogg", headers=_attachment(name))
    return FileResponse(path, media_type="audio/ogg", filename=name)


def _attachment(name: str) -> dict[str, str]:
    from urllib.parse import quote

    return {"content-disposition": f"attachment; filename*=utf-8''{quote(name)}"}


@router.post("/{session_id}/clips/{clip_id}/save")
async def save_clip(
    session_id: str,
    clip_id: str,
    body: SaveClipRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
):
    """Copy a clip to a path the desktop app's save dialog chose. Only for a client on this
    computer: in server mode the path would be on the server."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(status_code=403, detail="Saving to a path only works on this computer")
    source = _clip_path(ctx, session_id, clip_id)
    target = Path(body.path).expanduser()
    if not target.parent.is_dir():
        raise HTTPException(status_code=400, detail="That folder does not exist")
    if is_encrypted(source):
        if ctx.file_key is None:
            raise HTTPException(status_code=423, detail="Meetings are encrypted and locked")
        await asyncio.to_thread(EncryptedFile(source, ctx.file_key).write_plain, target)
    else:
        shutil.copyfile(source, target)
    return {"path": str(target)}
