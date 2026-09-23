"""Audio recording endpoints."""

import asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...audio.capture import list_devices, start_recording, stop_recording
from ...audio.mixer import mix_audio_files
from ...models.session import DEFAULT_SESSION_NAME, Recording, Session, SessionStatus
from ...services.pipeline import live_transcribe, transcribe_session
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])


class StartRecordingRequest(BaseModel):
    device_ids: list[int]
    session_id: str | None = None  # Existing session ID, or create new


class StartRecordingResponse(BaseModel):
    session_id: str
    recording_id: str
    live_job_id: str | None = None
    message: str


class StopRecordingRequest(BaseModel):
    transcribe: bool | None = None  # None = follow settings.auto_transcribe


class StopRecordingResponse(BaseModel):
    session: Session
    job_id: str | None
    message: str


async def _apply_calendar(ctx: AppContext, session: Session) -> Session:
    """Name an untitled session after the meeting in progress and keep its attendees.
    Calendar problems never block recording."""
    if not (ctx.calendar.configured and ctx.settings.calendar_auto_name):
        return session
    if session.name != DEFAULT_SESSION_NAME:
        return session
    try:
        event = await asyncio.wait_for(ctx.calendar.current(), timeout=5)
    except Exception:
        logger.warning("Calendar lookup failed at recording start", exc_info=True)
        return session
    if event is None:
        return session
    ctx.repo.update_fields(session.id, attendees=event.attendees)
    return ctx.sessions.rename_session(session.id, event.title) or session


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

    session = await _apply_calendar(ctx, session)

    if session.id in ctx.active_recordings:
        raise HTTPException(status_code=409, detail="Session is already recording")

    output_dir = ctx.settings.recordings_dir / session.id
    recording = await start_recording(request.device_ids, output_dir)
    if not recording.processes:
        raise HTTPException(status_code=400, detail="None of the selected devices could be opened")
    ctx.active_recordings[session.id] = recording
    ctx.sessions.set_status(session.id, SessionStatus.RECORDING)

    live_job_id = None
    if ctx.settings.live_transcription:
        job = ctx.jobs.submit("live", live_transcribe(ctx, session.id, recording), session.id)
        live_job_id = job.id

    return StartRecordingResponse(
        session_id=session.id,
        recording_id=recording.session_id,
        live_job_id=live_job_id,
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

    # Stop live transcription first so it does not race the encoder for the files.
    for job in ctx.jobs.list(session_id=session_id, active_only=True):
        if job.kind == "live":
            await ctx.jobs.cancel(job.id)

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


# ---- playback + import -------------------------------------------------------

_MEDIA_TYPES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


@router.get("/file/{session_id}")
async def get_audio(
    session_id: str, recording: str | None = None, ctx: AppContext = Depends(get_ctx)
):
    """Stream a session's audio (the mixed file by default, or one recording by id).
    Supports HTTP range requests so the player can seek."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    path = session.audio_file
    if recording:
        match = next((r for r in session.recordings if r.id == recording), None)
        if match is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        path = match.path
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="No audio file for this session")
    media = _MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=Path(path).name)


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
IMPORT_EXTENSIONS = {
    ".wav",
    ".ogg",
    ".opus",
    ".mp3",
    ".m4a",
    ".flac",
    ".webm",
    ".mp4",
    ".mkv",
    ".aac",
    ".wma",
}


@router.post("/import", response_model=StopRecordingResponse)
async def import_audio(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    transcribe: bool = Form(True),
    ctx: AppContext = Depends(get_ctx),
):
    """Create a session from an uploaded audio/video file and (by default) transcribe it."""
    original = Path(file.filename or "import")
    if original.suffix.lower() not in IMPORT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {original.suffix!r}")

    session = ctx.sessions.create_session(name or original.stem)
    out_dir = ctx.settings.recordings_dir / session.id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = out_dir / ("import_" + (_SAFE.sub("_", original.name) or "file"))
    with saved.open("wb") as f:
        shutil.copyfileobj(file.file, f, length=1024 * 1024)
    if saved.stat().st_size == 0:
        saved.unlink(missing_ok=True)
        ctx.sessions.delete_session(session.id)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    ctx.sessions.set_status(session.id, SessionStatus.ENCODING)
    try:
        mixed = mix_audio_files([saved], out_dir / "import_mixed.ogg")
    except Exception as e:
        ctx.sessions.set_status(session.id, SessionStatus.ERROR)
        raise HTTPException(status_code=400, detail=f"Could not decode audio: {e}") from e

    ctx.sessions.set_audio(
        session.id,
        str(mixed),
        [Recording(source="import", device_id=-1, device_name=original.name, path=str(saved))],
    )
    ctx.sessions.set_status(session.id, SessionStatus.CREATED)

    job_id = None
    if transcribe:
        job = ctx.jobs.submit("transcribe", transcribe_session(ctx, session.id), session.id)
        job_id = job.id
    return StopRecordingResponse(
        session=ctx.sessions.get_session(session.id),
        job_id=job_id,
        message=f"Imported {original.name}",
    )


# ---- echo cancellation ---------------------------------------------------------


class EchoCancelResponse(BaseModel):
    supported: bool
    reason: str | None
    active: bool
    enabled: bool  # the persisted setting
    source_node_id: int | None


class EchoCancelRequest(BaseModel):
    enabled: bool


async def _echo_response(ctx: AppContext) -> EchoCancelResponse:
    st = await ctx.echo.status()
    return EchoCancelResponse(
        supported=st.supported,
        reason=st.reason,
        active=st.active,
        enabled=ctx.settings.echo_cancel,
        source_node_id=st.source_node_id,
    )


@router.get("/echo-cancel", response_model=EchoCancelResponse)
async def echo_cancel_status(ctx: AppContext = Depends(get_ctx)):
    return await _echo_response(ctx)


@router.post("/echo-cancel", response_model=EchoCancelResponse)
async def echo_cancel_set(request: EchoCancelRequest, ctx: AppContext = Depends(get_ctx)):
    """Load or unload the PipeWire echo-cancel module and remember the choice."""
    from ...config import save_settings

    if request.enabled:
        st = await ctx.echo.start()
        if not st.active:
            raise HTTPException(
                status_code=400, detail=st.reason or "Could not start echo cancellation"
            )
    else:
        await ctx.echo.stop()
    if ctx.settings.echo_cancel != request.enabled:
        ctx.settings.echo_cancel = request.enabled
        save_settings(ctx.settings)
    return await _echo_response(ctx)
