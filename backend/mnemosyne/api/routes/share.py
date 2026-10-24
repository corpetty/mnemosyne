"""The hub side of sharing with other people (phase 8; docs/plans/2026-10-23-phase-8-tasks.md,
C1, C11, C14, C15): what a contact's computer asks of this one, with the CONTACT token this
person gave it at pairing. The auth middleware lets such a token reach only /api/share/ and
runs the request as the contact (`access.current`, user id "p:<16 hex>"); here that is turned
back into the contact. The admin token has no contact, so it is refused (403) like anyone else.
Not on a team server (409).
"""

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ... import access
from ...models.contact import Card
from ...models.share_hub import (
    SHARE_PROTOCOL,
    ShareChanges,
    ShareHello,
    ShareTombstone,
)
from ...models.shared_meeting import ContributionBatch, ContributionResponse
from ...services import contacts
from ...services.shares import (
    ShareNotFound,
    accept_contributions,
    batch_too_big,
    build_shared_meeting,
)
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)


def _no_team(ctx: AppContext = Depends(get_ctx)) -> None:
    if ctx.settings.team_mode:
        raise HTTPException(409, "Sharing with other people is for personal computers")


router = APIRouter(prefix="/api/share", tags=["share"], dependencies=[Depends(_no_team)])

PAGE_LIMIT = 100


def _contact(ctx: AppContext) -> dict:
    """The live contact calling, from the request's principal (403 for anyone else)."""
    who = access.principal()
    row = contacts.by_pid(ctx, who.user_id) if who is not None else None
    if row is None:
        raise HTTPException(403, "Only a contact of this person may do this")
    return row


def _page(ctx: AppContext, person: str, since: int, limit: int) -> tuple[list, int, bool]:
    """Blocking: build the signed meetings changed for `person`. The work is this computer's
    own (it folds live notes), so it runs without the contact's principal; what goes out is
    decided by `person`'s shares alone."""
    reset = access.current.set(None)
    try:
        changes, next_since, more = ctx.repo.shared_changes_since(person, since, limit)
        sessions = []
        for session_id, _seq in changes:
            try:
                sessions.append(build_shared_meeting(ctx, session_id, person))
            except ShareNotFound:
                continue  # unshared or deleted since the listing: its tombstone says so
            except Exception:
                logger.warning("Could not build shared meeting %s", session_id, exc_info=True)
                raise
        return sessions, next_since, more
    finally:
        access.current.reset(reset)


@router.get("/hello", response_model=ShareHello)
async def hello(request: Request, ctx: AppContext = Depends(get_ctx)):
    """Who this is and which sharing protocol it speaks; a recipient asks first in every round."""
    _contact(ctx)
    return ShareHello(
        protocol=SHARE_PROTOCOL,
        app_version=request.app.version,
        person=ctx.person.public_hex,
        name=contacts.display_name(ctx),
    )


@router.get("/changes", response_model=ShareChanges)
async def changes(
    since: int = Query(0, ge=0),
    tombstones_since: str = "",
    limit: int = Query(50, ge=1, le=PAGE_LIMIT),
    ctx: AppContext = Depends(get_ctx),
):
    """The meetings shared with the calling contact that changed after `since` (this computer's
    change counter), whole and signed, oldest change first; and what it must forget since
    `tombstones_since` (ISO time on this computer's clock): shares withdrawn and shared meetings
    deleted. Only this contact's shares are ever read."""
    row = _contact(ctx)
    person = row["person"]
    sessions, next_since, more = await asyncio.to_thread(_page, ctx, person, since, limit)
    tombstones = ctx.repo.shared_tombstones_since(person, tombstones_since)
    return ShareChanges(
        sessions=sessions,
        next_since=next_since,
        more=more,
        tombstones=[ShareTombstone(**t) for t in tombstones],
        tombstones_next=max((t["at"] for t in tombstones), default=tombstones_since),
    )


@router.post("/card", status_code=204)
async def card(body: Card, ctx: AppContext = Depends(get_ctx)):
    """The calling contact's newer card (its machines changed). Taken only when it is signed by
    that contact's own key and newer than the one held; its machines get their CONTACT
    devices. 400 for anything else."""
    row = _contact(ctx)
    if not contacts.verify_card(body, expected_person=row["person"]):
        raise HTTPException(400, "That card is not signed by this contact")
    if body.at <= row["card"].get("at", ""):
        raise HTTPException(400, "That card is not newer than the one held")
    try:
        contacts.upsert_contact(ctx, body, row["secret"])
    except contacts.ContactsRefused as e:
        raise HTTPException(400, str(e)) from e
    return Response(status_code=204)


@router.post("/contributions", response_model=ContributionResponse)
async def contributions(body: ContributionBatch, ctx: AppContext = Depends(get_ctx)):
    """What the calling contact did on meetings shared with them: signed records (comments,
    action-item ticks and stars, live notes frames), applied in order as that contact through
    the usual rules. One result per record; a refused record has a reason and the rest still
    go. Records are idempotent by id. 413 over 200 records or 2 MB of frames."""
    row = _contact(ctx)
    too_big = batch_too_big(body)
    if too_big:
        raise HTTPException(413, too_big)
    # The contact's principal is set inside the thread, by accept_contributions.
    results = await asyncio.to_thread(accept_contributions, ctx, row, body)
    return ContributionResponse(results=results)


WAIT_SECONDS = 10.0


@router.post("/poke", status_code=204)
async def poke(wait: bool = False, ctx: AppContext = Depends(get_ctx)):
    """The calling contact has news: what it shares with this person is pulled now, in the
    background (services/share_pull.py). A contact that shares nothing gives an empty round.
    `wait`: answer once that round is over (at most WAIT_SECONDS), for a contact that is about
    to be removed and whose token stops working right after: its tombstones must be pulled
    first."""
    row = _contact(ctx)
    if ctx.share_pull is not None:
        if wait:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(ctx.share_pull.pull(row["person"])), WAIT_SECONDS
                )
        else:
            ctx.share_pull.poke(row["person"])
    return Response(status_code=204)
