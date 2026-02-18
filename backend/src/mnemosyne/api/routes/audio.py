"""Audio recording endpoints, integrated with session management."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...audio.capture import RecordingSession, start_recording, stop_recording
from ...audio.mixer import mix_audio_files
from ...config import DATA_DIR
from ...models.session import SessionStatus
from ...services.session_service import session_service

router = APIRouter(prefix="/api/audio", tags=["audio"])

# In-memory store: maps app session ID -> recording session
_active_recordings: dict[str, RecordingSession] = {}


class StartRecordingRequest(BaseModel):
    device_ids: list[int]
    session_id: str | None = None  # Existing app session ID, or create new


class StartRecordingResponse(BaseModel):
    session_id: str
    recording_id: str
    message: str


class StopRecordingResponse(BaseModel):
    session_id: str
    recording_id: str
    output_file: str
    individual_files: list[str]
    message: str


@router.post("/start", response_model=StartRecordingResponse)
async def start(request: StartRecordingRequest):
    """Start recording from selected audio devices."""
    if not request.device_ids:
        raise HTTPException(status_code=400, detail="No devices selected")

    # Get or create app session
    if request.session_id:
        app_session = session_service.get_session(request.session_id)
        if app_session is None:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        app_session = session_service.create_session()

    # Start recording into a directory named after the app session
    output_dir = DATA_DIR / "recordings" / app_session.id
    recording = await start_recording(request.device_ids, output_dir)
    _active_recordings[app_session.id] = recording

    # Update session status
    session_service.set_status(app_session.id, SessionStatus.RECORDING)

    return StartRecordingResponse(
        session_id=app_session.id,
        recording_id=recording.session_id,
        message=f"Recording started from {len(recording.processes)} device(s)",
    )


@router.post("/stop/{session_id}", response_model=StopRecordingResponse)
async def stop(session_id: str):
    """Stop recording for an app session and mix audio sources."""
    recording = _active_recordings.get(session_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="No active recording for this session")

    if not recording.is_recording:
        raise HTTPException(status_code=400, detail="Session is not recording")

    individual_files = await stop_recording(recording)

    # Mix files if we have multiple sources
    mixed_path = recording.output_dir / f"{recording.session_id}_mixed.ogg"
    if individual_files:
        mixed_path = mix_audio_files(individual_files, mixed_path)

    recording_id = recording.session_id
    del _active_recordings[session_id]

    # Update app session with audio file path
    output_file = str(mixed_path) if mixed_path.exists() else ""
    if output_file:
        session_service.set_audio_file(session_id, output_file)
    session_service.set_status(session_id, SessionStatus.PROCESSING)

    return StopRecordingResponse(
        session_id=session_id,
        recording_id=recording_id,
        output_file=output_file,
        individual_files=[str(f) for f in individual_files],
        message=f"Recording stopped. {len(individual_files)} file(s) captured.",
    )


@router.get("/status/{session_id}")
async def status(session_id: str):
    """Check the status of a recording for an app session."""
    recording = _active_recordings.get(session_id)
    if recording is None:
        return {"session_id": session_id, "is_recording": False, "exists": False}

    return {
        "session_id": session_id,
        "is_recording": recording.is_recording,
        "exists": True,
        "device_count": len(recording.processes),
    }
