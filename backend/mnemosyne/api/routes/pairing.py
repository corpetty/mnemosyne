"""Pairing devices with this backend: one-time codes, the paired devices, removing them
(services/pairing.py), and remote access for computers (services/link.py). Redeeming a code
needs no token; everything else here but /me needs the API token or a computer's (api/auth.py)."""

import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import access
from ...models.base import ApiModel
from ...models.contact import Card
from ...models.sync import SyncPeerPayload, SyncPersonOffer, SyncRedeemSelf
from ...services import contacts, sync_peers
from ...services.pairing import (
    CONTACT_CODE,
    DESKTOP,
    PHONE,
    SYNC,
    InvalidPairingCode,
    PairedDevice,
)
from ..context import AppContext, get_ctx
from .mobile import phone_page_urls

logger = logging.getLogger(__name__)
_HEX64 = re.compile(r"[0-9a-f]{64}")
router = APIRouter(prefix="/api/pairing", tags=["pairing"])
ADMIN = [Depends(access.require_admin)]  # team-wide: an admin's job (access.py)


class CodeRequest(BaseModel):
    # sync: a desktop that also syncs; contact: another person's computer (services/contacts.py)
    kind: Literal["phone", "desktop", "sync", "contact"] = PHONE


class PairingCode(ApiModel):
    code: str
    kind: str
    expires_at: datetime
    urls: list[str]  # phone: the phone page with the code, one per address a phone might use
    invite: str | None  # desktop, sync or contact: what the other computer pastes (<ticket>#<code>)


class PairedDeviceInfo(ApiModel):
    id: str
    name: str
    kind: str  # "phone" or "desktop"
    created_at: datetime
    last_seen_at: datetime | None


class RedeemRequest(BaseModel):
    code: str
    name: str = ""
    endpoint_id: str | None = None  # set by the link sidecar for a computer
    peer: SyncPeerPayload | None = None  # a sync code: the other computer's id, ticket, token
    card: dict | None = None  # a contact code: {"card": <Card>, "secret": <64 hex>}


class RemoteAccess(ApiModel):
    enabled: bool  # the remote_access setting
    running: bool
    endpoint_id: str | None
    error: str | None


class RedeemResponse(ApiModel):
    device: PairedDeviceInfo
    token: str
    sync: SyncRedeemSelf | None  # a sync code: this computer's sync id, name and key offer
    card: dict | None = None  # a contact code: this person's card


def _info(d: PairedDevice) -> PairedDeviceInfo:
    return PairedDeviceInfo(
        id=d.id, name=d.name, kind=d.kind, created_at=d.created_at, last_seen_at=d.last_seen_at
    )


def make_code(ctx: AppContext, kind: str) -> PairingCode:
    """A one-time pairing code of `kind` and what the other side needs to redeem it (shared by
    this route and POST /api/contacts/codes)."""
    if not ctx.settings.api_token:
        raise HTTPException(
            status_code=409,
            detail="Set an API token first: without one, anyone who reaches the backend can use it",
        )
    ticket = ctx.link.status.ticket
    if kind == SYNC and ctx.settings.team_mode:
        raise HTTPException(
            status_code=409, detail="Library sync is for personal computers, not a team server"
        )
    if kind == CONTACT_CODE and ctx.settings.team_mode:
        raise HTTPException(
            status_code=409, detail="Contacts are for personal computers, not a team server"
        )
    if kind in (DESKTOP, SYNC, CONTACT_CODE) and not (ctx.link.status.running and ticket):
        raise HTTPException(status_code=409, detail="Turn on remote access first")
    code, expires = ctx.pairing.new_code(kind)
    return PairingCode(
        code=code,
        kind=kind,
        expires_at=datetime.fromtimestamp(expires, UTC),
        urls=[f"{u}?pair={code}" for u in phone_page_urls(ctx.settings)] if kind == PHONE else [],
        invite=f"{ticket}#{code}" if kind in (DESKTOP, SYNC, CONTACT_CODE) else None,
    )


@router.post("/codes", response_model=PairingCode, dependencies=ADMIN)
async def create_code(body: CodeRequest | None = None, ctx: AppContext = Depends(get_ctx)):
    return make_code(ctx, body.kind if body else PHONE)


async def _redeem_contact(body: RedeemRequest, ctx: AppContext) -> RedeemResponse:
    """A contact code: store the redeemer's card, write their CONTACT devices and answer with
    our card and the token they will present to us. Everything is checked before anything is
    stored or the code used up."""
    if ctx.settings.team_mode:
        raise HTTPException(
            status_code=409, detail="Contacts are for personal computers, not a team server"
        )
    if not ctx.settings.api_token:
        raise HTTPException(
            status_code=409,
            detail="Set an API token first: without one, anyone who reaches the backend can use it",
        )
    sent = body.card if isinstance(body.card, dict) else {}
    secret = sent.get("secret")
    if (
        not body.endpoint_id
        or not isinstance(secret, str)
        or not isinstance(sent.get("card"), dict)
    ):
        raise HTTPException(
            status_code=400, detail="A contact code needs the other person's card and secret"
        )
    try:
        card = Card.model_validate(sent["card"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail="That contact card is not valid") from e
    if not contacts.verify_card(card):
        raise HTTPException(status_code=400, detail="That contact card is not valid")
    if body.endpoint_id not in contacts.endpoints(card):
        raise HTTPException(
            status_code=400, detail="That contact card does not list the computer calling"
        )
    try:
        ours = contacts.build_card(ctx)
        row = contacts.upsert_contact(ctx, card, secret)
    except contacts.ContactsUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except contacts.ContactsRefused as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    device = next(
        (d for d in ctx.pairing.contacts_for(card.person) if d.endpoint_id == body.endpoint_id),
        None,
    )
    if device is None:  # a newer card of theirs is already stored and does not list this machine
        raise HTTPException(status_code=409, detail="A newer card of theirs is stored: try again")
    try:
        ctx.pairing.consume_code(body.code, CONTACT_CODE)
    except InvalidPairingCode as e:  # cannot happen between the check and here; be safe
        await contacts.forget_contact(ctx, card.person)
        raise HTTPException(status_code=403, detail=f"{e}: show a new code in Settings") from e
    _, token = contacts.tokens(secret, ctx.person.public_hex, card.person)
    await contacts.dial_all(ctx, row)  # tolerant: a machine not reachable yet is skipped
    logger.info("Paired with contact %s", contacts.my_or_card_name(row))
    return RedeemResponse(device=_info(device), token=token, sync=None, card=ours.model_dump())


def _person_offer(body: RedeemRequest, ctx: AppContext) -> SyncPersonOffer | None:
    """A sync code: who adopts whose person key (services/contacts.py `adoption`). None when the
    redeemer sent no key info (a computer from before the person key: paired as before). Raises
    409 when both computers have people data under different keys: they are different people."""
    sent = body.card.get("person") if isinstance(body.card, dict) else None
    if sent is None:
        return None
    pub = sent.get("pub") if isinstance(sent, dict) else None
    has = sent.get("has_people_data") if isinstance(sent, dict) else None
    if not (isinstance(pub, str) and _HEX64.fullmatch(pub) and isinstance(has, bool)):
        raise HTTPException(status_code=400, detail="That computer's person key info is not valid")
    mine = ctx.person.public_hex
    decision = contacts.adoption(mine, contacts.has_people_data(ctx), pub, has)
    if decision is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "These computers belong to different people: remove the people one of them "
                "shares with first"
            ),
        )
    return SyncPersonOffer(pub=mine, adopt=decision)


@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest, ctx: AppContext = Depends(get_ctx)):
    if ctx.pairing.code_kind(body.code) == CONTACT_CODE:
        return await _redeem_contact(body, ctx)
    syncing = ctx.pairing.code_kind(body.code) == SYNC
    offer: SyncPersonOffer | None = None
    if syncing:
        peer = body.peer
        if ctx.settings.team_mode:
            raise HTTPException(
                status_code=403, detail="Library sync is for personal computers, not a team server"
            )
        if peer is None or not (peer.id and peer.ticket and peer.token):
            raise HTTPException(
                status_code=400,
                detail="A sync code needs the other computer's id, ticket and token",
            )
        if body.endpoint_id and body.endpoint_id == ctx.link.status.endpoint_id:
            raise HTTPException(status_code=400, detail="A computer cannot sync with itself")
        offer = _person_offer(body, ctx)  # before the code is used: a refusal pairs nothing
    try:
        device, token = ctx.pairing.redeem(body.code, body.name, body.endpoint_id)
    except InvalidPairingCode as e:
        raise HTTPException(status_code=403, detail=f"{e}: show a new code in Settings") from e
    me = None
    if syncing and body.peer is not None and body.endpoint_id:
        for old in ctx.pairing.desktops_for(body.endpoint_id):  # paired again: one device
            if old.id != device.id:
                ctx.pairing.revoke(old.id)
        try:
            peer = await sync_peers.record_incoming(
                ctx, device_name=device.name, endpoint_id=body.endpoint_id, payload=body.peer
            )
        except sync_peers.SyncPairingError as e:
            ctx.pairing.revoke(device.id)  # nothing recorded, nothing trusted
            raise HTTPException(status_code=409, detail=str(e)) from e
        ctx.pairing.rename(device.id, peer.name)
        me = SyncRedeemSelf(**asdict(ctx.sync_peers.self_info()), person=offer)
    return RedeemResponse(device=_info(device), token=token, sync=me)


@router.get("/devices", response_model=list[PairedDeviceInfo], dependencies=ADMIN)
async def list_devices(ctx: AppContext = Depends(get_ctx)):
    return [_info(d) for d in ctx.pairing.devices()]


@router.delete("/devices/{device_id}", dependencies=ADMIN)
async def remove_device(device_id: str, ctx: AppContext = Depends(get_ctx)):
    device = ctx.pairing.get(device_id)
    if device is None or not ctx.pairing.revoke(device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    # A computer syncing with this one goes with its device (what it synced stays).
    peer = ctx.sync_peers.find_by_endpoint(device.endpoint_id) if device.endpoint_id else None
    if peer is not None:
        await sync_peers.forget(ctx, peer)
    return {"message": "Device removed"}


@router.get("/me", response_model=PairedDeviceInfo)
async def me(request: Request, ctx: AppContext = Depends(get_ctx)):
    """The paired device making the request (the phone page checks it is still paired)."""
    device = ctx.pairing.get(getattr(request.state, "device_id", "") or "")
    if device is None:
        raise HTTPException(status_code=404, detail="Not a paired device")
    return _info(device)


@router.get("/remote", response_model=RemoteAccess, dependencies=ADMIN)
async def remote_access(ctx: AppContext = Depends(get_ctx)):
    st = ctx.link.status
    return RemoteAccess(
        enabled=ctx.settings.remote_access,
        running=st.running,
        endpoint_id=st.endpoint_id,
        error=st.error,
    )
