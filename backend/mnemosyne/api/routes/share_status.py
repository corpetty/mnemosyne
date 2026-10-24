"""How my work on a meeting someone shared with me stands (phase 8, task 16;
services/share_push.py): what waits to be sent to its owner's computer and what they refused.
The desktop or an admin; the contact tokens never reach /api/sessions."""

from fastapi import APIRouter, Depends, HTTPException

from ...models.share_hub import ShareOwner, ShareRejected, ShareStatus, ShareWaiting
from ...models.shared_meeting import ShareParts
from ...services import contacts, share_push, shared_with_me
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}", tags=["sharing"])


@router.get("/share-status", response_model=ShareStatus)
async def share_status(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """404 for a meeting that is not shared with this person."""
    if not ctx.repo.exists(session_id):
        raise HTTPException(404, "Session not found")
    person = ctx.repo.shared_from(session_id)
    if not person:
        raise HTTPException(404, "This meeting is not shared with you")
    row = ctx.repo.contact(person)
    name = contacts.my_or_card_name(row) if row else ""
    waiting = share_push.waiting_for(ctx, session_id)
    return ShareStatus(
        owner=ShareOwner(person=person, name=name or contacts.pid_of(person)),
        parts=shared_with_me.parts_of(ctx, session_id, person) or ShareParts(),
        waiting=ShareWaiting(**waiting),
        rejected=[ShareRejected(**r) for r in share_push.rejected_for(ctx, session_id)],
    )
