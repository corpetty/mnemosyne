"""A recording handed over by a computer that uses this one (docs/plans/2026-11-09-connections-
design.md, "Uses my main computer"; task 6).

`POST /api/connections/handoff` is multipart: a JSON `meta` part and one audio file per source
and part, as the laptop recorded them (16-bit PCM WAV, as pw-record and the browser recorder
write, or the Ogg Opus a finished recording becomes). The meeting is made HERE (origin this
computer) and its audio goes through what a local recording goes through when it stops: each
part's sources are written as WAVs with a recovery manifest (services/recovery.py), then the
`finish` job encodes, mixes, joins the parts (services/parts.py `add_part`), seals them
(encryption at rest) and queues transcription per `auto_transcribe` (and so the summary per
`auto_summarize`). Answered as soon as the audio is on disk: from there a crash is recovered at
the next start like an interrupted recording, so the laptop may delete its copy.

Who: a paired computer (a DESKTOP device token) or this computer's own owner (its API token, or
the desktop app); never a phone, a contact, a team member, or a team server. Idempotent by
`handoff_id` (services/handoff.py).

With `session_id`, the hand-off REPLACES the audio of a meeting this same device was streaming
into (POST /api/audio/start-browser with its token: `by_device` in the history) because the
stream had gaps. Only before anything transcribed it, and only when every recording of it came
from that device's stream: otherwise 409. Replacing is the one exception to "never replace a
session's audio wholesale" (CLAUDE.md), and safe only then: no transcript, part or clip yet
depends on the old timeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, ValidationError
from starlette.datastructures import (
    UploadFile,
)  # what Request.form() gives (FastAPI's subclasses it)

from ...audio.capture import (
    BROWSER_SOURCES,
    BrowserProcess,
    RecordingProcess,
    RecordingSession,
)
from ...models.base import ApiModel
from ...models.session import DEFAULT_SESSION_NAME, SessionStatus
from ...services import handoff as handoffs
from ...services import history, records, shared_notes
from ...services.pairing import DESKTOP
from ...services.recovery import write_manifest
from ..context import AppContext, get_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connections", tags=["connections"])

# The whole request. A 3-hour meeting is about 1 GB per source as 48 kHz WAV; Ogg is far less.
MAX_HANDOFF_BYTES = 4 * 1024**3
MAX_META_BYTES = 8 * 1024 * 1024  # the JSON part, notes included
MAX_FILES = 64
_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
# Kinds of events after which a meeting's audio may no longer be replaced.
_TRANSCRIBED = {"transcribed", "transcribe_failed", "flagged_lines_removed"}
_NOT_A_STREAM = {"imported", "combined", "handed_over"}


class HandoffSource(ApiModel):
    source: Literal["mic", "system"]
    part: int = Field(ge=0, le=10_000)  # the laptop's part number; parts are joined in order
    offset: float = Field(default=0.0, ge=0)  # informational: add_part measures the timeline
    sample_rate: int = Field(ge=8000, le=192_000)
    file: str = Field(min_length=1, max_length=100)  # the multipart field holding its audio
    label: str = ""  # what the laptop calls the source ("MacBook microphone")


class HandoffMeta(ApiModel):
    handoff_id: str
    title: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str = ""  # markdown typed during the meeting; may be empty
    sources: list[HandoffSource] = Field(min_length=1, max_length=MAX_FILES)
    session_id: str | None = None  # replace the audio of the meeting this device streamed


class HandoffResult(ApiModel):
    session_id: str
    replaced: bool  # the audio of a streamed meeting was replaced (meta.session_id)


def _sender(ctx: AppContext, request: Request) -> tuple[str | None, str]:
    """(device id or None for this computer's owner, a name for the history). 403 for anyone
    else. The middleware already keeps phones (DEVICE_PATHS) and contacts (/api/share/) out."""
    state = request.scope.get("state") or {}
    if state.get("user_id"):
        raise HTTPException(status_code=403, detail="Only a paired computer can hand over")
    device_id = state.get("device_id")
    if device_id is None:
        return None, "this computer"
    device = ctx.pairing.get(device_id)
    if device is None or device.kind != DESKTOP:
        raise HTTPException(status_code=403, detail="Only a paired computer can hand over")
    return device.id, device.name


def _local_time(t: datetime | None) -> datetime | None:
    """Meetings keep naive local times (Session.created_at)."""
    if t is None or t.tzinfo is None:
        return t
    return t.astimezone().replace(tzinfo=None)


def _kind_of(head: bytes) -> str | None:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    return None


def to_wav(src: Path, dest: Path) -> None:
    """An Ogg (Opus) file as 16-bit mono WAV, what the finish job encodes (blocking)."""
    out = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1",
         "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, text=True, timeout=3600,
    )  # fmt: skip
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg: {out.stderr[-300:]}")


def _write_source(upload: UploadFile, kind: str, dest: Path) -> None:
    """The upload as `dest` (a WAV), blocking."""
    upload.file.seek(0)
    if kind == "wav":
        with dest.open("wb") as f:
            shutil.copyfileobj(upload.file, f, length=1024 * 1024)
        return
    raw = dest.with_suffix(".handoff.ogg")
    try:
        with raw.open("wb") as f:
            shutil.copyfileobj(upload.file, f, length=1024 * 1024)
        to_wav(raw, dest)
    finally:
        raw.unlink(missing_ok=True)


def _check_replace(ctx: AppContext, session_id: str, device_id: str | None):
    """The meeting whose audio this hand-off replaces, or 404/409 saying why not."""
    session = ctx.sessions.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if device_id is None or session.origin or session.shared_from:
        raise HTTPException(status_code=409, detail="Only the computer that streamed it")
    events = ctx.repo.events(session_id)
    started = [e for e in events if e["kind"] == "recording_started"]
    if not started or any((e["detail"] or {}).get("by_device") != device_id for e in started):
        raise HTTPException(
            status_code=409, detail="This meeting was not recorded from this computer's stream"
        )
    if any(e["kind"] in _NOT_A_STREAM for e in events):
        raise HTTPException(status_code=409, detail="This meeting has audio from elsewhere too")
    if session_id in ctx.active_recordings or session_id in ctx.starting:
        raise HTTPException(status_code=409, detail="The meeting is still recording")
    transcribing = any(j.kind == "transcribe" for j in ctx.jobs.list(session_id=session_id))
    if (
        session.transcript
        or transcribing
        or session.status == SessionStatus.TRANSCRIBING
        or any(e["kind"] in _TRANSCRIBED for e in events)
    ):
        raise HTTPException(
            status_code=409, detail="Transcription has started: the audio can no longer be replaced"
        )
    return session


def _sealed_audio(ctx: AppContext, session_id: str) -> bool:
    """Does the meeting's last seal list any audio file (so replacing it is an erasure)?"""
    seals = ctx.repo.seals(session_id)
    if not seals:
        return False
    last = seals[-1]
    listed = last["audio"] if last["format"] == 1 else (last["manifest"] or {}).get("audio")
    return bool(listed)


async def _seal_replaced_audio(ctx: AppContext, session, sender: str) -> None:
    """Seal the erasure of the streamed audio a hand-off replaces, BEFORE anything changes: the
    record's chain lets an audio file leave only by being erased (services/records.py). Audio no
    seal ever listed is just replaced (nothing to erase from). 409 with the reason when the
    erasure cannot be sealed (encryption locked, an unreadable file)."""
    if not _sealed_audio(ctx, session.id):
        return

    def log() -> int:
        return ctx.repo.log_deletion(
            session, "audio", sender, "system", f"Replaced by the full recording from {sender}"
        )

    try:
        await asyncio.to_thread(
            records.seal_erasure,
            ctx,
            session.id,
            by=sender,
            reason=f"Replaced by the full recording handed over from {sender}",
            deletion=log,
        )
    except records.ErasureNotSealed as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _parts(meta: HandoffMeta, out_dir: Path) -> list[RecordingSession]:
    """One recording per part, in part order, numbered 0.. as this meeting's parts."""
    by_part: dict[int, list[HandoffSource]] = {}
    for s in meta.sources:
        by_part.setdefault(s.part, []).append(s)
    recordings = []
    for number, part in enumerate(sorted(by_part)):
        rec = RecordingSession(session_id=secrets.token_hex(4), output_dir=out_dir)
        rec.part = number
        for s in by_part[part]:
            process = BrowserProcess()
            process.terminate()  # finished: the finish job only saves it
            default = "Microphone" if s.source == "mic" else "Call audio"
            rec.processes.append(
                RecordingProcess(
                    device_id=BROWSER_SOURCES[s.source],
                    process=process,
                    output_path=out_dir / f"{rec.session_id}_handoff_{s.source}.wav",
                    source=s.source,
                    label=(s.label.strip() or default)[:80],
                )
            )
        recordings.append(rec)
    return recordings


def _finish_all(ctx: AppContext, session_id: str, recordings, old_files: list[Path]):
    """The finish job of a hand-off: each part saved as a stopped recording is, in order; the
    last one queues transcription per `auto_transcribe`. Replaced audio is deleted once the
    new audio is in the meeting."""
    from .audio import _finish_recording

    want = ctx.settings.auto_transcribe

    async def run(job) -> dict:
        result: dict = {}
        for i, rec in enumerate(recordings):
            last = i == len(recordings) - 1
            result = await _finish_recording(ctx, session_id, rec, want and last)(job)
        session = ctx.sessions.get_session(session_id)
        keep = {r.path for r in session.recordings} | {session.audio_file} if session else set()
        for path in old_files:
            if str(path) not in keep:
                path.unlink(missing_ok=True)
        return {**result, "parts": len(recordings)}

    return run


@router.post("/handoff", response_model=HandoffResult)
async def handoff(request: Request, ctx: AppContext = Depends(get_ctx)):
    """A finished recording from a paired computer becomes a meeting here (multipart: `meta`
    JSON {handoff_id, title, started_at, ended_at, notes, sources: [{source, part, offset,
    sample_rate, file, label}], session_id?} plus one WAV or Ogg file per source and part, in
    the field each source names). Idempotent by handoff_id. 413 over 4 GB."""
    device_id, sender = _sender(ctx, request)
    if ctx.settings.team_mode:
        raise HTTPException(status_code=409, detail="A team server does not take hand-offs")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_HANDOFF_BYTES:
        raise HTTPException(status_code=413, detail="A hand-off is at most 4 GB")
    try:
        form = await request.form(
            max_files=MAX_FILES, max_fields=MAX_FILES + 8, max_part_size=MAX_META_BYTES
        )
    except Exception as e:  # noqa: BLE001 - malformed multipart (Starlette's own errors)
        raise HTTPException(status_code=400, detail=f"Not a hand-off: {e}") from e
    try:
        return await _receive(ctx, form, device_id, sender)
    finally:
        await form.close()


async def _receive(ctx: AppContext, form, device_id: str | None, sender: str) -> HandoffResult:
    raw = form.get("meta")
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="No meta part")
    try:
        meta = HandoffMeta.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        raise HTTPException(status_code=400, detail=f"Bad meta: {e}") from e
    if not _ID.match(meta.handoff_id):
        raise HTTPException(status_code=400, detail="Bad handoff_id")
    if len({(s.source, s.part) for s in meta.sources}) != len(meta.sources):
        raise HTTPException(status_code=400, detail="A source appears twice in one part")

    store = handoffs.store(ctx)
    key = store.key(device_id, meta.handoff_id)
    done = store.session_for(key)
    if done is not None:  # a retry: the answer was lost, the work was not
        return HandoffResult(session_id=done, replaced=bool(meta.session_id))
    if not store.claim(key):
        raise HTTPException(status_code=409, detail="This hand-off is being received; try again")
    try:
        return await _take(ctx, form, meta, device_id, sender, key, store)
    finally:
        store.release(key)


async def _take(ctx, form, meta: HandoffMeta, device_id, sender, key, store) -> HandoffResult:
    uploads: dict[str, tuple[UploadFile, str]] = {}
    total = 0
    for s in meta.sources:
        upload = form.get(s.file)
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail=f"No file in field {s.file!r}")
        head = await upload.read(12)
        kind = _kind_of(head)
        if kind is None:
            raise HTTPException(status_code=400, detail=f"{s.file!r} is neither WAV nor Ogg")
        total += upload.size or 0
        uploads[s.file] = (upload, kind)
    if total > MAX_HANDOFF_BYTES:
        raise HTTPException(status_code=413, detail="A hand-off is at most 4 GB")

    replaced = meta.session_id is not None
    old_files: list[Path] = []
    if replaced:
        session = _check_replace(ctx, meta.session_id, device_id)
        # A finish (or recovery) of the stream itself may still be saving: after it, check again.
        for job in ctx.jobs.list(session_id=session.id, active_only=True):
            if job.kind in ("finish", "recover"):
                await ctx.jobs.wait(job.id)
        session = _check_replace(ctx, meta.session_id, device_id)
        if ctx.jobs.list(session_id=session.id, active_only=True):
            raise HTTPException(status_code=409, detail="The meeting is busy; try again soon")
        # Before any file is written or anything cleared: a refusal leaves the meeting as it was.
        await _seal_replaced_audio(ctx, session, sender)
    else:
        title = meta.title.strip()[:200] or DEFAULT_SESSION_NAME
        session = ctx.sessions.create_session(title, created_at=_local_time(meta.started_at))
    sid = session.id
    out_dir = ctx.settings.recordings_dir / sid
    out_dir.mkdir(parents=True, exist_ok=True)
    recordings = _parts(meta, out_dir)
    paths = {}
    for rec in recordings:
        for proc in rec.processes:
            paths[(proc.source, rec.part)] = proc.output_path
    order = sorted({s.part for s in meta.sources})
    try:
        for s in meta.sources:
            upload, kind = uploads[s.file]
            dest = paths[(s.source, order.index(s.part))]
            await asyncio.to_thread(_write_source, upload, kind, dest)
    except Exception as e:
        for dest in paths.values():
            dest.unlink(missing_ok=True)
        if not replaced:
            ctx.sessions.delete_session(sid)
        raise HTTPException(status_code=400, detail=f"Could not read the audio: {e}") from e

    if replaced:
        old_files = [Path(r.path) for r in session.recordings]
        if session.audio_file:
            old_files.append(Path(session.audio_file))
        # A part of the stream whose save failed waits for recovery: it is replaced too.
        new = {rec.session_id for rec in recordings}
        for manifest in out_dir.glob("recording-*.json"):
            if manifest.stem.removeprefix("recording-") in new:
                continue
            try:
                tracks = json.loads(manifest.read_text()).get("tracks", [])
                old_files += [out_dir / Path(t["wav"]).name for t in tracks]
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            manifest.unlink(missing_ok=True)
        ctx.repo.clear_audio(sid)
    for rec in recordings:  # from here a crash is recovered at the next start
        write_manifest(rec, {})
    ctx.sessions.set_status(sid, SessionStatus.ENCODING)
    if meta.notes.strip() and not (replaced and session.notes.strip()):
        # A streamed meeting's notes were typed into it live; a hand-off's come with it.
        with shared_notes.unrestricted():
            ctx.sessions.update_notes(sid, meta.notes)
    history.log(
        ctx,
        sid,
        "handed_over",
        None,
        by=sender,
        device_id=device_id,
        handoff_id=meta.handoff_id,
        parts=len(recordings),
        started_at=meta.started_at.isoformat() if meta.started_at else None,
        ended_at=meta.ended_at.isoformat() if meta.ended_at else None,
        replaced=replaced,
    )
    store.remember(key, sid)
    ctx.jobs.submit("finish", _finish_all(ctx, sid, recordings, old_files), sid)
    logger.info("Session %s: %d part(s) handed over by %s", sid, len(recordings), sender)
    return HandoffResult(session_id=sid, replaced=replaced)
