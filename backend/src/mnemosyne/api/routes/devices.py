"""Audio device listing endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...audio.capture import list_devices

router = APIRouter(prefix="/api/devices", tags=["devices"])


class DeviceResponse(BaseModel):
    id: int
    name: str
    description: str
    media_class: str
    is_input: bool
    is_output: bool
    is_monitor: bool
    is_echo_cancelled: bool = False


@router.get("", response_model=list[DeviceResponse])
async def get_devices():
    try:
        devices = list_devices()
    except (RuntimeError, FileNotFoundError) as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return [
        DeviceResponse(
            id=d.id,
            name=d.name,
            description=d.description,
            media_class=d.media_class,
            is_input=d.is_input,
            is_output=d.is_output,
            is_monitor=d.is_monitor,
            is_echo_cancelled=d.is_echo_cancelled,
        )
        for d in devices
    ]
