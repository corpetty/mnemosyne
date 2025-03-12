from fastapi import APIRouter, Query
from ..audio.capture import AudioCapture

router = APIRouter()

@router.get("/devices")
async def get_devices(force_refresh: bool = Query(False, description="Force refresh of device list")):
    print("Fetching devices...")
    devices = AudioCapture.list_devices(force_refresh=force_refresh)
    print(f"Devices found: {devices}")
    return {"devices": devices}
