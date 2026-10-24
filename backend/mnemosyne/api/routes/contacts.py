"""Contacts: other people whose computers this library shares meetings with
(services/contacts.py, docs/plans/2026-10-23-phase-8-tasks.md). Pairing uses a `contact` code
from POST /api/contacts/codes; the other person redeems it through the usual
POST /api/pairing/redeem. Admin only, and not on a team server."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...models.contact import (
    ContactInfo,
    ContactPairRequest,
    ContactRename,
    ContactsMe,
    ContactsResponse,
)
from ...services import contacts
from ...services.pairing import CONTACT_CODE
from ..context import AppContext, get_ctx
from .pairing import PairingCode, make_code

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/contacts", tags=["contacts"], dependencies=[Depends(access.require_admin)]
)


def _refusal(e: contacts.ContactsRefused) -> HTTPException:
    """Not available here (team server, remote access off) is a conflict with the current state;
    anything else the person gave us was wrong."""
    unavailable = isinstance(e, contacts.ContactsUnavailable) or "team server" in str(e)
    return HTTPException(status_code=409 if unavailable else 400, detail=str(e))


def _live(ctx: AppContext, person: str) -> dict:
    row = ctx.repo.contact(person)
    if row is None or row["removed_at"]:
        raise HTTPException(status_code=404, detail="No such contact")
    return row


@router.get("", response_model=ContactsResponse)
async def list_contacts(ctx: AppContext = Depends(get_ctx)):
    try:
        contacts.require_available(ctx)
    except contacts.ContactsRefused as e:
        raise _refusal(e) from e
    return ContactsResponse(
        me=ContactsMe(
            person=ctx.person.public_hex,
            pid=ctx.person.pid,
            name=contacts.display_name(ctx),
            fingerprint=ctx.person.fingerprint,
        ),
        contacts=[contacts.info(ctx, row) for row in ctx.repo.contacts()],
    )


@router.post("/codes", response_model=PairingCode)
async def create_code(ctx: AppContext = Depends(get_ctx)):
    """A one-time contact code: the other person pastes the invite in their own app."""
    return make_code(ctx, CONTACT_CODE)


@router.post("/pair", response_model=ContactInfo)
async def pair(body: ContactPairRequest, ctx: AppContext = Depends(get_ctx)):
    try:
        row = await contacts.pair_with_invite(ctx, body.invite)
    except contacts.ContactsRefused as e:
        raise _refusal(e) from e
    return contacts.info(ctx, row)


@router.patch("/{person}", response_model=ContactInfo)
async def rename(person: str, body: ContactRename, ctx: AppContext = Depends(get_ctx)):
    """Call a contact what you like here; an empty name goes back to the one on their card."""
    try:
        contacts.require_available(ctx)
        _live(ctx, person)
        row = await contacts.rename_contact(ctx, person, body.name)
    except contacts.ContactsRefused as e:
        raise _refusal(e) from e
    if row is None:
        raise HTTPException(status_code=404, detail="No such contact")
    return contacts.info(ctx, row)


@router.delete("/{person}")
async def remove(person: str, ctx: AppContext = Depends(get_ctx)):
    try:
        contacts.require_available(ctx)
        row = _live(ctx, person)
        await contacts.forget_contact(ctx, person)
    except contacts.ContactsRefused as e:
        raise _refusal(e) from e
    logger.info("Removed contact %s", row["name"])
    return {"message": "Contact removed"}
