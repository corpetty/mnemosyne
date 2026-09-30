"""Encryption at rest: status, turning it on and off, and unlocking with the recovery code."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...models.base import ApiModel
from ...services import encryption
from ...services.encryption import EncryptionEnabled, EncryptionStatus
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/encryption", tags=["encryption"])
ADMIN = [Depends(access.require_admin)]  # firm-wide: an admin's job (access.py)


class RecoveryRequest(ApiModel):
    recovery_code: str


def _idle(ctx: AppContext) -> None:
    if ctx.active_recordings or ctx.jobs.list(active_only=True):
        raise HTTPException(status_code=409, detail="Wait until recordings and jobs have finished")


@router.get("", response_model=EncryptionStatus)
async def get_status(ctx: AppContext = Depends(get_ctx)):
    return encryption.status(ctx)


@router.post("/enable", response_model=EncryptionEnabled, dependencies=ADMIN)
async def enable(ctx: AppContext = Depends(get_ctx)):
    """Encrypt the database and every audio file; returns the recovery code (shown once)."""
    _idle(ctx)
    try:
        return await asyncio.to_thread(encryption.enable, ctx)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/disable", response_model=EncryptionStatus, dependencies=ADMIN)
async def disable(ctx: AppContext = Depends(get_ctx)):
    """Decrypt everything again and forget the key."""
    _idle(ctx)
    try:
        await asyncio.to_thread(encryption.disable, ctx)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return encryption.status(ctx)


@router.post("/unlock", response_model=EncryptionStatus)
async def unlock(request: RecoveryRequest, ctx: AppContext = Depends(get_ctx)):
    """The keyring lost the key (new computer, reset keyring): open the meetings with the
    recovery code, and keep the key in the keyring again."""
    if not ctx.locked:
        return encryption.status(ctx)
    try:
        master = encryption.check_code(ctx, request.recovery_code)
        await ctx.unlock(master)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # a code that passes the check but does not open the database
        raise HTTPException(status_code=400, detail=f"Could not open the meetings: {e}") from e
    return encryption.status(ctx)
