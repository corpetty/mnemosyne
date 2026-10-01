"""Sharing a meeting on a team server (services/sharing.py): who may read it besides its owner."""

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...models.base import ApiModel
from ...services import sharing
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/sessions/{session_id}/shares", tags=["sharing"])


class SharePerson(ApiModel):
    id: str
    name: str


class Shares(ApiModel):
    team: bool  # shared with everyone on the server
    people: list[SharePerson]  # shared with these, by name
    can_change: bool  # the owner or an admin


class SharesUpdate(ApiModel):
    team: bool = False
    user_ids: list[str] = []


def _shares(ctx: AppContext, session_id: str) -> Shares:
    names = {u.id: u.name for u in ctx.users.list()}
    ids = [s["user_id"] for s in ctx.repo.shares(session_id)]
    owner = ctx.repo.owner_of(session_id) or ""
    return Shares(
        team=sharing.EVERYONE in ids,
        people=[SharePerson(id=i, name=names[i]) for i in ids if i in names],
        can_change=access.can_write(owner),
    )


def _readable(ctx: AppContext, session_id: str) -> None:
    if not ctx.repo.exists(session_id):
        raise HTTPException(status_code=404, detail="Session not found")


@router.get("", response_model=Shares)
async def get_shares(session_id: str, ctx: AppContext = Depends(get_ctx)):
    _readable(ctx, session_id)
    return _shares(ctx, session_id)


@router.put("", response_model=Shares)
async def put_shares(session_id: str, body: SharesUpdate, ctx: AppContext = Depends(get_ctx)):
    """Share with exactly these people (and everyone, with `team`). The owner or an admin."""
    _readable(ctx, session_id)
    if not access.can_write(ctx.repo.owner_of(session_id) or ""):
        raise access.Forbidden("Only the meeting's owner or an admin can share it")
    wanted = set(body.user_ids) | ({sharing.EVERYONE} if body.team else set())
    sharing.set_shares(ctx, session_id, wanted)
    return _shares(ctx, session_id)
