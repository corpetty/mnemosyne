"""Sharing a meeting with a contact, as its owner (phase 8; services/shares.py builds what the
contact receives): who has it, what they have of it, and stopping. The meeting's owner or an
admin (the desktop app counts as unrestricted); a meeting shared with you cannot be shared
onward. Not on a team server (409)."""

from fastapi import APIRouter, Depends, HTTPException, Response

from ... import access
from ...models.share_hub import PeerShareInfo
from ...models.shared_meeting import ShareParts
from ...services import contacts, history
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/peer-shares", tags=["sharing"])


def _owner_check(ctx: AppContext, session_id: str) -> None:
    """409 on a team server, 404 for a meeting that is not there, 403 unless the caller may
    change it, and 403 for one shared with this person (only its owner shares it onward)."""
    if ctx.settings.team_mode:
        raise HTTPException(409, "Sharing with other people is for personal computers")
    if not ctx.repo.exists(session_id):
        raise HTTPException(404, "Session not found")
    if ctx.repo._shared_from(session_id):
        raise access.Forbidden(
            f"Shared by {ctx.repo._sharer_name(session_id)}: only its owner can share it"
        )
    ctx.repo._check_write(session_id)


def _live_contact(ctx: AppContext, person: str) -> dict:
    row = ctx.repo.contact(person)
    if row is None or row["removed_at"]:
        raise HTTPException(404, "No such contact")
    return row


def _info(row: dict | None, share: dict, person: str) -> PeerShareInfo:
    name = contacts.my_or_card_name(row) if row else ""
    return PeerShareInfo(
        person=person,
        name=name or contacts.pid_of(person),
        parts=ShareParts.from_stored(share["parts"]),
        at=share["at"],
        updated_at=share["updated_at"],
    )


def _summary(parts: ShareParts) -> list[str]:
    """The parts that are on, by name (the history's detail)."""
    on = [k for k, v in parts.model_dump().items() if v and k != "hide_identifiers"]
    return on + (["hide_identifiers"] if parts.hide_identifiers else [])


@router.get("", response_model=list[PeerShareInfo])
async def list_peer_shares(session_id: str, ctx: AppContext = Depends(get_ctx)):
    _owner_check(ctx, session_id)
    return [
        _info(ctx.repo.contact(s["person"]), s, s["person"])
        for s in ctx.repo.peer_shares(session_id)
    ]


@router.put("/{person}", response_model=PeerShareInfo)
async def put_peer_share(
    session_id: str, person: str, body: ShareParts, ctx: AppContext = Depends(get_ctx)
):
    """Share the meeting with this contact (or change what they have of it). The recipient's
    next pull gets the new document."""
    _owner_check(ctx, session_id)
    row = _live_contact(ctx, person)
    existed = ctx.repo.peer_share(session_id, person)
    changing = existed is not None and not existed["revoked_at"]
    who = access.principal()
    share = ctx.repo.put_peer_share(session_id, person, body.model_dump(), who.name if who else "")
    history.log(
        ctx,
        session_id,
        "share_changed" if changing else "shared_with",
        name=contacts.my_or_card_name(row),
        parts=_summary(body),
    )
    return _info(row, share, person)


@router.delete("/{person}", status_code=204)
async def delete_peer_share(session_id: str, person: str, ctx: AppContext = Depends(get_ctx)):
    """Stop sharing the meeting with this contact; their next pull tells them to forget it."""
    _owner_check(ctx, session_id)
    if not ctx.repo.revoke_peer_share(session_id, person):
        raise HTTPException(404, "Not shared with that contact")
    row = ctx.repo.contact(person)
    history.log(ctx, session_id, "unshared", name=contacts.my_or_card_name(row) if row else "")
    return Response(status_code=204)
