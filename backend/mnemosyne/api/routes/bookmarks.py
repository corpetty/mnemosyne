"""Bookmarks: moments marked as important, while recording or afterwards."""

import time

from fastapi import APIRouter, Depends, HTTPException

from ...models.base import ApiModel
from ...models.session import Bookmark
from ...services.parts import offsets, part_of
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/bookmarks", tags=["bookmarks"])


class BookmarkCreate(ApiModel):
    # Seconds on the meeting's timeline; leave it out while recording to mark "now"
    # (`mnemosyne --mark`, the tray, the Mark button).
    at: float | None = None
    note: str = ""


class BookmarkUpdate(ApiModel):
    note: str


def _publish(ctx: AppContext, session_id: str) -> None:
    ctx.bus.publish({"type": "bookmarks", "session_id": session_id})


@router.post("", response_model=Bookmark)
async def add(session_id: str, request: BookmarkCreate, ctx: AppContext = Depends(get_ctx)):
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    recording = ctx.active_recordings.get(session_id)
    if request.at is None:
        if recording is None or not recording.is_recording:
            raise HTTPException(status_code=409, detail="Not recording: give the time to mark")
        part, seconds = recording.part, time.time() - recording.started_at
    else:
        starts = offsets(session)
        part = part_of(request.at, starts)
        seconds = request.at - starts.get(part, 0.0)
    bookmark_id = ctx.repo.add_bookmark(session_id, part, seconds, request.note.strip())
    _publish(ctx, session_id)
    marked = next(b for b in ctx.sessions.get_session(session_id).bookmarks if b.id == bookmark_id)
    if request.at is None and part not in offsets(session) and session.transcript:
        # A later part still being recorded: it starts after what the meeting has so far.
        marked.at = round(max(s.end for s in session.transcript) + seconds, 2)
    return marked


@router.patch("/{bookmark_id}", response_model=Bookmark)
async def update(
    session_id: str, bookmark_id: str, request: BookmarkUpdate, ctx: AppContext = Depends(get_ctx)
):
    if not ctx.repo.update_bookmark(session_id, bookmark_id, request.note.strip()):
        raise HTTPException(status_code=404, detail="Bookmark not found")
    _publish(ctx, session_id)
    return next(b for b in ctx.sessions.get_session(session_id).bookmarks if b.id == bookmark_id)


@router.delete("/{bookmark_id}")
async def delete(session_id: str, bookmark_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.repo.delete_bookmark(session_id, bookmark_id):
        raise HTTPException(status_code=404, detail="Bookmark not found")
    _publish(ctx, session_id)
    return {"deleted": True}
