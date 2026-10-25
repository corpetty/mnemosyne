"""Guest notes (services/guest_notes.py): a meeting's live notes opened to people without the
app, through a room on a publish host or on this computer over iroh (phase 10). The owner's API
under /api/sessions/{id}/guest-notes.

Who: anyone who can see the meeting reads the room's state; its link (which carries the key)
only goes to the meeting's owner and admins, readers get `url: null` and no guest names. Starting,
ending, a new link and stopping are the owner's or an admin's (a team server also needs
`allow_guest_notes`, to start). The guests' page itself is served beside the publication
viewer (routes/publishing.py, /n/<room>).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...models.guest_notes import GuestRoom
from ...services import guest_notes
from ..context import AppContext, get_ctx

router = APIRouter(tags=["guest-notes"])


def _session(ctx: AppContext, session_id: str):
    session = ctx.repo.get(session_id)  # a meeting the caller may not see: Hidden (404)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _by() -> str:
    who = access.principal()
    return who.name if who else ""


def _model(ctx: AppContext, row: dict, session) -> GuestRoom:
    state = guest_notes.state(row)
    owner = access.can_write(session.owner_id)
    origin = guest_notes.page_origin(ctx)
    return GuestRoom(
        url=guest_notes.link(row, origin) if owner and state != "stopped" else None,
        host_url=row["host_url"],
        state=state,
        created_at=row["created_at"],
        ended_at=row["ended_at"],
        expires_at=row["expires_at"],
        started_by=row["started_by"],
        guests_present=guest_notes.guests_present(ctx, session.id) if owner else [],
        transport=row.get("transport", "host"),
        relay=row.get("relay", ""),
        relay_kind=guest_notes.relay_kind(row.get("relay", "")),
        public_url=row.get("public_url", ""),
        mirror_host_url=row.get("mirror_host_url", ""),
        page_origin=guest_notes.served_from(row, origin),
    )


def _may_change(session) -> None:
    if not access.can_write(session.owner_id):
        raise access.Forbidden("Only the meeting's owner or an admin can change its guest notes")


async def _run(coro):
    try:
        return await coro
    except guest_notes.GuestNotesUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except guest_notes.GuestNotesError as e:  # the host refused or cannot be reached
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/api/sessions/{session_id}/guest-notes", response_model=GuestRoom | None)
async def get_guest_notes(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """The meeting's guest notes room (live or ended), or null (200) when there is none or it
    was stopped. The link only for its owner or an admin."""
    session = _session(ctx, session_id)
    row = ctx.repo.guest_room(session_id)
    if row is None or guest_notes.state(row) == "stopped":
        return None
    return _model(ctx, row, session)


@router.put("/api/sessions/{session_id}/guest-notes", response_model=GuestRoom)
async def start_guest_notes(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Open the meeting's notes to guests: the live room, or a new one (an ended room is
    replaced). 403 when not the owner (or, on a team server, not allowed there), 409 when it
    cannot start (no host, no sidecar, a shared meeting), 502 when the host refuses."""
    session = _session(ctx, session_id)
    row = await _run(guest_notes.start(ctx, session_id, by=_by()))
    return _model(ctx, row, session)


@router.post("/api/sessions/{session_id}/guest-notes/end", response_model=GuestRoom)
async def end_guest_notes(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """End the room: guests keep reading until it expires (24 h), nobody can type. 404 when
    the meeting has no live room."""
    session = _session(ctx, session_id)
    _may_change(session)
    row = await _run(guest_notes.end(ctx, session_id, by=_by()))
    if row is None:
        raise HTTPException(status_code=404, detail="This meeting has no live guest notes")
    return _model(ctx, row, session)


@router.post("/api/sessions/{session_id}/guest-notes/new-link", response_model=GuestRoom)
async def new_guest_notes_link(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """A new room and key: the old link stops working and its guests are gone. 404 when the
    meeting has no guest notes."""
    session = _session(ctx, session_id)
    _may_change(session)
    row = ctx.repo.guest_room(session_id)
    if row is None or guest_notes.state(row) == "stopped":
        raise HTTPException(status_code=404, detail="This meeting has no guest notes")
    row = await _run(guest_notes.new_link(ctx, session_id, by=_by()))
    return _model(ctx, row, session)


@router.delete("/api/sessions/{session_id}/guest-notes", response_model=GuestRoom)
async def stop_guest_notes(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Stop: the room is deleted from its host at once, live or ended, and the link dies.
    Returns the room with state "stopped" (no link). 404 when there is none."""
    session = _session(ctx, session_id)
    _may_change(session)
    row = await _run(guest_notes.stop(ctx, session_id, by=_by()))
    if row is None:
        raise HTTPException(status_code=404, detail="This meeting has no guest notes")
    return _model(ctx, row, session)
