"""Pairing phones with this backend in server mode: one-time codes, the paired devices, and
removing them (services/pairing.py). Redeeming a code needs no token; everything else here
but /me needs the API token (api/auth.py)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...models.base import ApiModel
from ...services.pairing import InvalidPairingCode, PairedDevice
from ..context import AppContext, get_ctx
from .mobile import phone_page_urls

router = APIRouter(prefix="/api/pairing", tags=["pairing"])


class PairingCode(ApiModel):
    code: str
    expires_at: datetime
    urls: list[str]  # the phone page with the code, one per address a phone might use


class PairedDeviceInfo(ApiModel):
    id: str
    name: str
    created_at: datetime
    last_seen_at: datetime | None


class RedeemRequest(BaseModel):
    code: str
    name: str = ""


class RedeemResponse(ApiModel):
    device: PairedDeviceInfo
    token: str


def _info(d: PairedDevice) -> PairedDeviceInfo:
    return PairedDeviceInfo(
        id=d.id, name=d.name, created_at=d.created_at, last_seen_at=d.last_seen_at
    )


@router.post("/codes", response_model=PairingCode)
async def create_code(ctx: AppContext = Depends(get_ctx)):
    if not ctx.settings.api_token:
        raise HTTPException(
            status_code=409,
            detail="Set an API token first: without one, anyone who reaches the backend can use it",
        )
    code, expires = ctx.pairing.new_code()
    return PairingCode(
        code=code,
        expires_at=datetime.fromtimestamp(expires, UTC),
        urls=[f"{u}?pair={code}" for u in phone_page_urls(ctx.settings)],
    )


@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest, ctx: AppContext = Depends(get_ctx)):
    try:
        device, token = ctx.pairing.redeem(body.code, body.name)
    except InvalidPairingCode as e:
        raise HTTPException(status_code=403, detail=f"{e}: show a new code in Settings") from e
    return RedeemResponse(device=_info(device), token=token)


@router.get("/devices", response_model=list[PairedDeviceInfo])
async def list_devices(ctx: AppContext = Depends(get_ctx)):
    return [_info(d) for d in ctx.pairing.devices()]


@router.delete("/devices/{device_id}")
async def remove_device(device_id: str, ctx: AppContext = Depends(get_ctx)):
    if not ctx.pairing.revoke(device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    return {"message": "Device removed"}


@router.get("/me", response_model=PairedDeviceInfo)
async def me(request: Request, ctx: AppContext = Depends(get_ctx)):
    """The paired device making the request (the phone page checks it is still paired)."""
    device = ctx.pairing.get(getattr(request.state, "device_id", "") or "")
    if device is None:
        raise HTTPException(status_code=404, detail="Not a paired device")
    return _info(device)
