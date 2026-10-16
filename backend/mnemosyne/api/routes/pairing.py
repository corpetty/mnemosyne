"""Pairing devices with this backend: one-time codes, the paired devices, removing them
(services/pairing.py), and remote access for computers (services/link.py). Redeeming a code
needs no token; everything else here but /me needs the API token or a computer's (api/auth.py)."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import access
from ...models.base import ApiModel
from ...models.sync import SyncPeerPayload, SyncSelf
from ...services import sync_peers
from ...services.pairing import DESKTOP, PHONE, SYNC, InvalidPairingCode, PairedDevice
from ..context import AppContext, get_ctx
from .mobile import phone_page_urls

router = APIRouter(prefix="/api/pairing", tags=["pairing"])
ADMIN = [Depends(access.require_admin)]  # team-wide: an admin's job (access.py)


class CodeRequest(BaseModel):
    kind: Literal["phone", "desktop", "sync"] = PHONE  # sync: a desktop that also syncs


class PairingCode(ApiModel):
    code: str
    kind: str
    expires_at: datetime
    urls: list[str]  # phone: the phone page with the code, one per address a phone might use
    invite: str | None  # desktop or sync: what the other computer pastes (<ticket>#<code>)


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


class RemoteAccess(ApiModel):
    enabled: bool  # the remote_access setting
    running: bool
    endpoint_id: str | None
    error: str | None


class RedeemResponse(ApiModel):
    device: PairedDeviceInfo
    token: str
    sync: SyncSelf | None  # a sync code: this computer's sync id and name


def _info(d: PairedDevice) -> PairedDeviceInfo:
    return PairedDeviceInfo(
        id=d.id, name=d.name, kind=d.kind, created_at=d.created_at, last_seen_at=d.last_seen_at
    )


@router.post("/codes", response_model=PairingCode, dependencies=ADMIN)
async def create_code(body: CodeRequest | None = None, ctx: AppContext = Depends(get_ctx)):
    kind = body.kind if body else PHONE
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
    if kind in (DESKTOP, SYNC) and not (ctx.link.status.running and ticket):
        raise HTTPException(status_code=409, detail="Turn on remote access first")
    code, expires = ctx.pairing.new_code(kind)
    return PairingCode(
        code=code,
        kind=kind,
        expires_at=datetime.fromtimestamp(expires, UTC),
        urls=[f"{u}?pair={code}" for u in phone_page_urls(ctx.settings)] if kind == PHONE else [],
        invite=f"{ticket}#{code}" if kind in (DESKTOP, SYNC) else None,
    )


@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest, ctx: AppContext = Depends(get_ctx)):
    syncing = ctx.pairing.code_kind(body.code) == SYNC
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
    try:
        device, token = ctx.pairing.redeem(body.code, body.name, body.endpoint_id)
    except InvalidPairingCode as e:
        raise HTTPException(status_code=403, detail=f"{e}: show a new code in Settings") from e
    me = None
    if syncing and body.peer is not None and body.endpoint_id:
        for old in ctx.pairing.desktops_for(body.endpoint_id):  # paired again: one device
            if old.id != device.id:
                ctx.pairing.revoke(old.id)
        peer = await sync_peers.record_incoming(
            ctx, device_name=device.name, endpoint_id=body.endpoint_id, payload=body.peer
        )
        ctx.pairing.rename(device.id, peer.name)
        me = SyncSelf(**asdict(ctx.sync_peers.self_info()))
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
