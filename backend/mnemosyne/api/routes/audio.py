"""Audio recording endpoints."""

import asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from ...audio.capture import list_devices, start_recording, stop_capture, stop_recording
from ...audio.health import CaptureHealth
from ...audio.levels import Level, SelfTestResult, sample_level, self_test
from ...audio.mixer import mix_audio_files
from ...audio.streams import CaptureApp
from ...models.base import ApiModel
from ...models.session import DEFAULT_SESSION_NAME, Recording, Session, SessionStatus
from ...models.transcript import TranscriptSegment
from ...services.copilot import copilot_runner
from ...services.encryption import seal_session_audio
from ...services.parts import add_part, next_part
from ...services.pipeline import live_transcribe, transcribe_session
from ...services.recovery import write_manifest
from ...storage.crypto import EncryptedFile, is_encrypted
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"])


class StartRecordingRequest(ApiModel):
    device_ids: list[int]
    session_id: str | None = None  # Existing session ID, or create new


class StartRecordingResponse(ApiModel):
    session_id: str
    recording_id: str
    live_job_id: str | None = None
    message: str


class StopRecordingRequest(ApiModel):
    transcribe: bool | None = None  # None = follow settings.auto_transcribe


class StopRecordingResponse(ApiModel):
    session: Session
    # Stop: the `finish` job (encode, mix, then transcribe when will_transcribe).
    # Import: the transcription job, if any.
    job_id: str | None
    will_transcribe: bool = False
    message: str


async def _stream_levels(ctx: AppContext, session_id: str, recording, interval: float = 0.25):
    """Publish per-device input levels while a recording runs (for the UI meters)."""
    from ...audio.levels import level_of
    from ...transcription.live import WavTail

    tails = {p.device_id: WavTail(p.output_path) for p in recording.processes}
    ids = [p.device_id for p in recording.processes]
    health = CaptureHealth({d: recording.labels.get(d, str(d)) for d in ids})
    try:
        while True:
            await asyncio.sleep(interval)
            levels = {}
            for proc in recording.processes:
                try:
                    pcm = tails[proc.device_id].read_new()
                    levels[str(proc.device_id)] = level_of(pcm).model_dump()
                except Exception:
                    pcm = None
                exited = recording.is_recording and proc.process.returncode is not None
                change = health.update(proc.device_id, 0 if pcm is None else pcm.size, exited)
                if change is not None:
                    _report_health(ctx, session_id, recording, health, change)
            if levels:
                ctx.bus.publish({"type": "levels", "session_id": session_id, "levels": levels})
    except asyncio.CancelledError:
        pass


def _report_health(ctx: AppContext, session_id: str, recording, health, change) -> None:
    recording.problems = health.problems()
    log = logger.info if change.state == "ok" else logger.warning
    log("Session %s: %s", session_id, change.message)
    ctx.bus.publish(
        {
            "type": "capture_health",
            "session_id": session_id,
            "device_id": change.device_id,
            "state": change.state,
            "message": change.message,
        }
    )


async def _apply_calendar(ctx: AppContext, session: Session) -> Session:
    """Name an untitled session after the meeting in progress and keep its attendees.
    Calendar problems never block recording."""
    if not (ctx.calendar.configured and ctx.settings.calendar_auto_name):
        return session
    if session.name != DEFAULT_SESSION_NAME:
        return session
    try:
        # A stale copy of the feed is fine for "what is on now"; never wait for the network.
        event = await asyncio.wait_for(ctx.calendar.current(stale_ok=True), timeout=5)
    except Exception:
        logger.warning("Calendar lookup failed at recording start", exc_info=True)
        return session
    if event is None:
        return session
    from ...models.session import AgendaItem
    from ...services.calendar_service import agenda_from_description

    fields: dict = {"attendees": event.attendees}
    if not session.agenda and (points := agenda_from_description(event.description)):
        fields["agenda"] = [AgendaItem(text=p) for p in points]
    ctx.repo.update_fields(session.id, **fields)
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
    return await begin_recording(ctx, session, request.device_ids)


async def begin_recording(
    ctx: AppContext, session: Session, device_ids: list[int], keep_notes: bool = False
) -> StartRecordingResponse:
    device_ids = await _apply_echo_mic(ctx, device_ids)
    output_dir = ctx.settings.recordings_dir / session.id
    recording = await start_recording(device_ids, output_dir)
    if not recording.processes:
        raise HTTPException(status_code=400, detail="None of the selected devices could be opened")
    # Recording again into a meeting that has audio adds a part; nothing is replaced.
    recording.part = next_part(session)
    try:
        devices = {d.id: d for d in await asyncio.to_thread(list_devices)}
    except Exception:
        devices = {}
    for proc in recording.processes:
        if (device := devices.get(proc.device_id)) is not None:
            recording.node_names[proc.device_id] = device.name
            recording.labels[proc.device_id] = device.description
    try:
        write_manifest(recording, devices)
    except Exception:  # recovery then falls back to one "mic" track per file
        logger.warning("Could not write the recording manifest", exc_info=True)
    ctx.active_recordings[session.id] = recording
    ctx.sessions.set_status(session.id, SessionStatus.RECORDING)
    ctx.level_tasks[session.id] = asyncio.create_task(_stream_levels(ctx, session.id, recording))

    live_job_id = None
    if ctx.settings.live_transcription:
        job = ctx.jobs.submit("live", live_transcribe(ctx, session.id, recording), session.id)
        live_job_id = job.id
        if ctx.settings.copilot:
            if not keep_notes:  # a new recording; a restarted capture carries on
                ctx.copilot_notes.pop(session.id, None)
                ctx.repo.update_fields(session.id, copilot_notes=None)
            ctx.jobs.submit("copilot", copilot_runner(ctx, session.id), session.id)

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
    """Stop capturing at once and answer; a `finish` job then encodes each source, mixes,
    seals and (by default) queues transcription. Encoding a long meeting takes a while (about
    18 s for 30 minutes), and the UI should not wait for it."""
    recording = ctx.active_recordings.get(session_id)
    if recording is None or not recording.is_recording:
        raise HTTPException(status_code=404, detail="No active recording for this session")
    transcribe = request.transcribe if request is not None else None
    job, want_transcribe = await stop_active(ctx, session_id, transcribe)
    return StopRecordingResponse(
        session=ctx.sessions.get_session(session_id),
        job_id=job.id,
        will_transcribe=want_transcribe,
        message="Recording stopped; saving it",
    )


async def stop_active(ctx: AppContext, session_id: str, transcribe: bool | None = None):
    """Stop an active recording now and queue its `finish` job. Returns (job, will_transcribe).
    Also used when the app went away and nobody came back for the recording (api/app_watch.py)."""
    recording = ctx.active_recordings[session_id]
    await stop_capture(recording)
    ctx.active_recordings.pop(session_id, None)
    task = ctx.level_tasks.pop(session_id, None)
    if task is not None:
        task.cancel()
    ctx.sessions.set_status(session_id, SessionStatus.ENCODING)

    want_transcribe = ctx.settings.auto_transcribe if transcribe is None else transcribe
    job = ctx.jobs.submit(
        "finish", _finish_recording(ctx, session_id, recording, want_transcribe), session_id
    )
    return job, want_transcribe


def _finish_recording(ctx: AppContext, session_id: str, recording, want_transcribe: bool):
    """Job runner: the slow part of stopping a recording."""

    async def run(job) -> dict:
        # Live transcription reads the WAVs and makes a last pass: let it finish before the
        # encoder replaces them.
        for other in ctx.jobs.list(session_id=session_id, active_only=True):
            if other.kind in ("live", "copilot"):
                await ctx.jobs.cancel(other.id)

        job.update("Encoding the recording", progress=0.1)
        try:
            devices = {d.id: d for d in await asyncio.to_thread(list_devices)}
        except Exception:
            devices = {}
        individual_files = await stop_recording(recording)
        if not individual_files:
            ctx.sessions.set_status(session_id, SessionStatus.ERROR)
            raise RuntimeError("Recording produced no audio")
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

        job.update("Mixing the sources", progress=0.6)
        mixed_path = await asyncio.to_thread(
            mix_audio_files,
            individual_files,
            recording.output_dir / f"{recording.session_id}_mixed.ogg",
        )
        had_transcript = bool(ctx.sessions.get_session(session_id).transcript)
        if recording.part:
            job.update("Adding it to the meeting", progress=0.75)
        await add_part(ctx, session_id, recording.part, recordings, mixed_path)
        job.update("Saving", progress=0.9)
        await asyncio.to_thread(seal_session_audio, ctx, session_id)
        ctx.sessions.set_status(session_id, SessionStatus.CREATED)

        transcribe_job = None
        if want_transcribe:
            # A later part of a transcribed meeting: transcribe only the new part.
            parts = [recording.part] if recording.part and had_transcript else None
            transcribe_job = ctx.jobs.submit(
                "transcribe", transcribe_session(ctx, session_id, parts), session_id
            ).id
        return {"sources": len(individual_files), "transcribe_job_id": transcribe_job}

    return run


@router.post("/restart/{session_id}", response_model=StartRecordingResponse)
async def restart(session_id: str, ctx: AppContext = Depends(get_ctx)):
    """Capture failed partway (a recorder stopped, or a source went quiet: audio/health.py).
    Save what was recorded and go on recording into the same meeting as its next part, from
    the same devices, found again by name (a recreated node has a new id)."""
    recording = ctx.active_recordings.get(session_id)
    if recording is None or not recording.is_recording:
        raise HTTPException(status_code=404, detail="No active recording for this session")
    ids = [p.device_id for p in recording.processes]
    names = dict(recording.node_names)
    job, _ = await stop_active(ctx, session_id, transcribe=False)
    saved = await ctx.jobs.wait(job.id)  # the next part starts where this one ends
    if saved is not None and saved.status != "completed":
        logger.warning("Session %s: saving before the restart failed: %s", session_id, saved.error)
    if ctx.settings.echo_cancel and not ctx.echo.active:  # it may be what went away
        try:
            await ctx.echo.start(_wanted_mic(ctx))
        except Exception as e:
            logger.warning("Echo canceller not restarted: %s", e)
    try:
        present = await asyncio.to_thread(list_devices)
    except Exception:
        present = []
    by_name = {d.name: d.id for d in present}
    ids = [by_name.get(names.get(i, ""), i) for i in ids]
    if present:
        ids = [i for i in ids if i in {d.id for d in present}]
    if not ids:
        raise HTTPException(status_code=400, detail="None of the recording's devices is there")
    session = ctx.sessions.get_session(session_id)
    return await begin_recording(ctx, session, ids, keep_notes=True)


class RecordingStatus(ApiModel):
    session_id: str
    is_recording: bool
    exists: bool
    device_count: int | None = None


class LiveSegment(ApiModel):
    source: str
    segment: TranscriptSegment


class ActiveRecording(ApiModel):
    """A recording in progress, so a UI that (re)connects can show it: after the app was
    restarted, the backend may still be recording (api/app_watch.py)."""

    session_id: str
    started_at: float  # unix time
    device_ids: list[int]
    part: int
    live: bool  # live transcription is running
    live_segments: list[LiveSegment] = []  # the live transcript so far
    problems: dict[str, str] = {}  # device id -> "stopped" | "stalled" (audio/health.py)


@router.get("/active", response_model=list[ActiveRecording])
async def active(ctx: AppContext = Depends(get_ctx)):
    out = []
    for session_id, recording in ctx.active_recordings.items():
        if not recording.is_recording:
            continue
        live = ctx.live.get(session_id)
        segments = (
            [
                LiveSegment(source=kind, segment=seg)
                for seg, kind in zip(live.committed, live.committed_kind, strict=False)
            ]
            if live is not None
            else []
        )
        out.append(
            ActiveRecording(
                session_id=session_id,
                started_at=recording.started_at,
                device_ids=[p.device_id for p in recording.processes],
                part=recording.part,
                live=live is not None,
                live_segments=segments,
                problems={str(d): state for d, state in recording.problems.items()},
            )
        )
    return out


@router.get("/status/{session_id}", response_model=RecordingStatus)
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


def encrypted_response(request: Request, path: Path, key: bytes | None, filename: str) -> Response:
    """Serve an encrypted file's plaintext, honouring Range requests (the player seeks).
    Open-ended ranges get at most 4 MiB at a time."""
    if key is None:
        raise HTTPException(status_code=423, detail="Meetings are encrypted and locked")
    f = EncryptedFile(path, key)
    media = _MEDIA_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")
    headers = {"accept-ranges": "bytes", "content-disposition": f'inline; filename="{filename}"'}
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("range") or "").strip())
    if m and (m.group(1) or m.group(2)):
        first, last = m.groups()
        if first:
            start = int(first)
            end = int(last) if last else min(f.size - 1, start + 4 * 1024 * 1024 - 1)
        else:  # the last N bytes
            start, end = max(0, f.size - int(last)), f.size - 1
        end = min(end, f.size - 1)
        if start > end:
            return Response(status_code=416, headers={"content-range": f"bytes */{f.size}"})
        headers["content-range"] = f"bytes {start}-{end}/{f.size}"
        return Response(f.read(start, end + 1), 206, media_type=media, headers=headers)
    return Response(f.read(), media_type=media, headers=headers)


@router.get("/file/{session_id}")
async def get_audio(
    session_id: str,
    request: Request,
    recording: str | None = None,
    ctx: AppContext = Depends(get_ctx),
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
    if is_encrypted(path):
        name = Path(path).name[: -len(".enc")]
        return await asyncio.to_thread(encrypted_response, request, Path(path), ctx.file_key, name)
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
    # What phones record: iOS camera and voice memos, Android recorders.
    ".mov",
    ".caf",
    ".3gp",
    ".3ga",
    ".amr",
    ".aif",
    ".aiff",
}


@router.post("/import", response_model=StopRecordingResponse)
async def import_audio(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    transcribe: bool = Form(True),
    session_id: str | None = Form(None),
    ctx: AppContext = Depends(get_ctx),
):
    """Create a session from an uploaded audio/video file and (by default) transcribe it. With
    `session_id`, the file is added to that meeting as its next part instead."""
    original = Path(file.filename or "import")
    if original.suffix.lower() not in IMPORT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type {original.suffix!r}")
    if session_id:
        return await _import_part(ctx, session_id, file, original, transcribe)

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
        mixed = await asyncio.to_thread(mix_audio_files, [saved], out_dir / "import_mixed.ogg")
    except Exception as e:
        ctx.sessions.set_status(session.id, SessionStatus.ERROR)
        raise HTTPException(status_code=400, detail=f"Could not decode audio: {e}") from e

    ctx.sessions.set_audio(
        session.id,
        str(mixed),
        [Recording(source="import", device_id=-1, device_name=original.name, path=str(saved))],
    )
    await asyncio.to_thread(seal_session_audio, ctx, session.id)
    ctx.sessions.set_status(session.id, SessionStatus.CREATED)

    job_id = None
    if transcribe:
        job = ctx.jobs.submit("transcribe", transcribe_session(ctx, session.id), session.id)
        job_id = job.id
    return StopRecordingResponse(
        session=ctx.sessions.get_session(session.id),
        job_id=job_id,
        will_transcribe=transcribe,
        message=f"Imported {original.name}",
    )


async def _import_part(ctx: AppContext, session_id: str, file, original: Path, transcribe: bool):
    """An audio file added to a meeting as its next part (after what it has)."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session_id in ctx.active_recordings or ctx.jobs.list(
        session_id=session_id, active_only=True
    ):
        raise HTTPException(status_code=409, detail="The meeting is busy; try again soon")
    part = next_part(session)
    out_dir = ctx.settings.recordings_dir / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = out_dir / (f"import{part}_" + (_SAFE.sub("_", original.name) or "file"))
    with saved.open("wb") as f:
        shutil.copyfileobj(file.file, f, length=1024 * 1024)
    if saved.stat().st_size == 0:
        saved.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    try:
        mixed = await asyncio.to_thread(
            mix_audio_files, [saved], out_dir / f"import{part}_mixed.ogg"
        )
    except Exception as e:
        saved.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not decode audio: {e}") from e
    had_transcript = bool(session.transcript)
    recording = Recording(source="import", device_id=-1, device_name=original.name, path=str(saved))
    await add_part(ctx, session_id, part, [recording], mixed)
    await asyncio.to_thread(seal_session_audio, ctx, session_id)

    job_id = None
    if transcribe:
        parts = [part] if part and had_transcript else None  # only the new part
        job_id = ctx.jobs.submit(
            "transcribe", transcribe_session(ctx, session_id, parts), session_id
        ).id
    return StopRecordingResponse(
        session=ctx.sessions.get_session(session_id),
        job_id=job_id,
        will_transcribe=transcribe,
        message=f"Added {original.name} to the meeting",
    )


# ---- echo cancellation ---------------------------------------------------------


class EchoCancelResponse(ApiModel):
    supported: bool
    reason: str | None
    active: bool
    enabled: bool  # the persisted setting
    source_node_id: int | None
    mic: str | None  # node.name the running canceller captures from; None = default source
    mic_description: str | None  # its display name, None when not connected or default
    # A different mic was chosen while recording: used from the next recording on.
    pending_mic: str | None


class EchoCancelRequest(ApiModel):
    enabled: bool
    # Microphone node.name to capture from; "" = the default source; omitted = keep the saved one.
    mic: str | None = None


def _wanted_mic(ctx: AppContext) -> str | None:
    return ctx.settings.echo_cancel_mic or None


async def _echo_response(ctx: AppContext) -> EchoCancelResponse:
    st = await ctx.echo.status()
    description = None
    if st.mic:
        try:
            description = next((d.description for d in list_devices() if d.name == st.mic), None)
        except Exception:
            logger.debug("Could not list devices for the echo canceller's mic", exc_info=True)
    pending = _wanted_mic(ctx) if st.active and _wanted_mic(ctx) != st.mic else None
    return EchoCancelResponse(
        supported=st.supported,
        reason=st.reason,
        active=st.active,
        enabled=ctx.settings.echo_cancel,
        source_node_id=st.source_node_id,
        mic=st.mic,
        mic_description=description,
        pending_mic=pending,
    )


async def _apply_echo_mic(ctx: AppContext, device_ids: list[int]) -> list[int]:
    """Before a recording starts: restart the echo canceller on a mic chosen since it last
    started (never mid-recording). The echo-cancelled source gets a new node id when it
    restarts, so the requested device ids are updated to match."""
    from ...audio.echo_cancel import find_source_node

    if not (ctx.echo.active and ctx.settings.echo_cancel) or ctx.echo.mic == _wanted_mic(ctx):
        return device_ids
    old_id = find_source_node()
    try:
        st = await ctx.echo.start(_wanted_mic(ctx))
    except ValueError as e:
        logger.warning("%s", e)
        return device_ids
    if old_id is None or st.source_node_id is None:
        return device_ids
    return [st.source_node_id if d == old_id else d for d in device_ids]


@router.get("/echo-cancel", response_model=EchoCancelResponse)
async def echo_cancel_status(ctx: AppContext = Depends(get_ctx)):
    return await _echo_response(ctx)


@router.post("/echo-cancel", response_model=EchoCancelResponse)
async def echo_cancel_set(request: EchoCancelRequest, ctx: AppContext = Depends(get_ctx)):
    """Load or unload the PipeWire echo-cancel module and remember the choice."""
    from ...audio.echo_cancel import valid_mic
    from ...config import save_settings

    if request.mic is not None:
        try:
            valid_mic(request.mic)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    changed = False
    if request.mic is not None and request.mic != ctx.settings.echo_cancel_mic:
        ctx.settings.echo_cancel_mic = request.mic
        changed = True
    if request.enabled:
        # A new mic while recording waits for the next recording (see _apply_echo_mic).
        if not (ctx.echo.active and ctx.active_recordings):
            st = await ctx.echo.start(_wanted_mic(ctx))
            if not st.active:
                raise HTTPException(
                    status_code=400, detail=st.reason or "Could not start echo cancellation"
                )
    else:
        await ctx.echo.stop()
    if ctx.settings.echo_cancel != request.enabled:
        ctx.settings.echo_cancel = request.enabled
        changed = True
    if changed:
        save_settings(ctx.settings)
    return await _echo_response(ctx)


# ---- levels and the capture self-test ------------------------------------------


def _device_or_404(device_id: int):
    device = next((d for d in list_devices() if d.id == device_id), None)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


@router.get("/level/{device_id}", response_model=Level)
async def device_level(device_id: int, seconds: float = 1.0, ctx: AppContext = Depends(get_ctx)):
    """Record briefly from a device and report its level (for checking a mic)."""
    seconds = min(max(seconds, 0.2), 5.0)
    return await sample_level(_device_or_404(device_id), seconds)


class SelfTestRequest(ApiModel):
    device_id: int


@router.post("/self-test", response_model=SelfTestResult)
async def capture_self_test(request: SelfTestRequest, ctx: AppContext = Depends(get_ctx)):
    """Check system-audio capture from an output: record it as a real recording would,
    play a short quiet tone through it, and report what was captured and from where."""
    device = _device_or_404(request.device_id)
    if not device.is_output:
        raise HTTPException(status_code=400, detail="Choose an output device to test")
    if ctx.active_recordings:
        raise HTTPException(status_code=409, detail="Stop the current recording first")
    return await self_test(device)


@router.get("/apps", response_model=list[CaptureApp])
async def get_capture_apps(ctx: AppContext = Depends(get_ctx)):
    """Other apps recording audio right now (meeting apps, browsers in a call), as of the
    last poll. Empty while auto_record is off."""
    return ctx.capture_apps_now
