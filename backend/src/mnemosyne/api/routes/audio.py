"""Audio recording endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...audio.capture import list_devices, start_recording, stop_recording
from ...audio.mixer import mix_audio_files
from ...models.session import Recording, Session, SessionStatus
from ...services.pipeline import transcribe_session
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/audio", tags=["audio"])


class StartRecordingRequest(BaseModel):
    device_ids: list[int]
    session_id: str | None = None  # Existing session ID, or create new


class StartRecordingResponse(BaseModel):
    session_id: str
    recording_id: str
    message: str


class StopRecordingRequest(BaseModel):
    transcribe: bool | None = None  # None = follow settings.auto_transcribe


class StopRecordingResponse(BaseModel):
    session: Session
    job_id: str | None
    message: str


@router.post("/start", response_model=StartRecordingResponse)
async def start(request: StartRecordingRequest, ctx: AppContext = Depends(get_ctx)):
    if not request.device_ids:
        raise HTTPException(status_code=400, detail="No devices selected")

    if request.session_id:
        session = ctx.sessions.get_session(request.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session = ctx.sessions.create_session()

    if session.id in ctx.active_recordings:
        raise HTTPException(status_code=409, detail="Session is already recording")

    output_dir = ctx.settings.recordings_dir / session.id
    recording = await start_recording(request.device_ids, output_dir)
    if not recording.processes:
        raise HTTPException(status_code=400, detail="None of the selected devices could be opened")
    ctx.active_recordings[session.id] = recording
    ctx.sessions.set_status(session.id, SessionStatus.RECORDING)

    return StartRecordingResponse(
        session_id=session.id,
        recording_id=recording.session_id,
        message=f"Recording started from {len(recording.processes)} device(s)",
    )


@router.post("/stop/{session_id}", response_model=StopRecordingResponse)
async def stop(
    session_id: str,
    request: StopRecordingRequest | None = None,
    ctx: AppContext = Depends(get_ctx),
):
    """Stop recording, encode each source, mix, and (by default) queue transcription."""
    recording = ctx.active_recordings.get(session_id)
    if recording is None or not recording.is_recording:
        raise HTTPException(status_code=404, detail="No active recording for this session")

    ctx.sessions.set_status(session_id, SessionStatus.ENCODING)
    devices = {d.id: d for d in list_devices()}
    individual_files = await stop_recording(recording)
    ctx.active_recordings.pop(session_id, None)

    recordings: list[Recording] = []
    for proc, path in zip(recording.processes, individual_files, strict=False):
        device = devices.get(proc.device_id)
        recordings.append(
            Recording(
                source="system" if (device is not None and device.is_output) else "mic",
                device_id=proc.device_id,
                device_name=device.description if device else str(proc.device_id),
                path=str(path),
            )
        )

    if not individual_files:
        ctx.sessions.set_status(session_id, SessionStatus.ERROR)
        raise HTTPException(status_code=500, detail="Recording produced no audio")

    mixed_path = mix_audio_files(
        individual_files, recording.output_dir / f"{recording.session_id}_mixed.ogg"
    )
    ctx.sessions.set_audio(session_id, str(mixed_path), recordings)
    ctx.sessions.set_status(session_id, SessionStatus.CREATED)

    want_transcribe = ctx.settings.auto_transcribe
    if request is not None and request.transcribe is not None:
        want_transcribe = request.transcribe

    job_id = None
    if want_transcribe:
        job = ctx.jobs.submit("transcribe", transcribe_session(ctx, session_id), session_id)
        job_id = job.id

    session = ctx.sessions.get_session(session_id)
    return StopRecordingResponse(
        session=session,
        job_id=job_id,
        message=f"Recording stopped. {len(individual_files)} source(s) captured.",
    )


@router.get("/status/{session_id}")
async def status(session_id: str, ctx: AppContext = Depends(get_ctx)):
    recording = ctx.active_recordings.get(session_id)
    if recording is None:
        return {"session_id": session_id, "is_recording": False, "exists": False}
    return {
        "session_id": session_id,
        "is_recording": recording.is_recording,
        "exists": True,
        "device_count": len(recording.processes),
    }
