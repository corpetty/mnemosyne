"""Recover recordings interrupted by a crash, a freeze or a forced restart.

While recording, pw-record writes one WAV per device; `stop` turns them into Opus files and
mixes them. When the backend dies first, the session stays `recording`, the WAVs stay behind
(often with a header whose sizes were never filled in) and pw-record may even keep writing.
On startup we stop such recorders, repair the headers and finish what `stop` would have done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..audio.capture import AudioDevice, RecordingSession, convert_to_opus
from ..audio.mixer import mix_audio_files
from ..models.base import ApiModel
from ..models.session import Recording, SessionStatus

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..jobs import JobContext

logger = logging.getLogger(__name__)

MANIFEST = "recording.json"  # before 0.10.1: one per folder, overwritten by the next recording


def manifest_path(recording: RecordingSession) -> Path:
    """One manifest per recording, so a recording whose save failed is not forgotten when the
    meeting is recorded into again; removed once the recording is saved."""
    return recording.output_dir / f"recording-{recording.session_id}.json"


INTERRUPTED = (SessionStatus.RECORDING, SessionStatus.ENCODING)


class RecoveredRecording(ApiModel):
    session_id: str
    name: str
    seconds: float
    transcribing: bool


def write_manifest(recording: RecordingSession, devices: dict[int, AudioDevice]) -> None:
    """Remember which file is which source. Device ids change after a reboot, so this cannot
    be looked up at recovery time."""
    from ..audio.capture import label_of, source_of

    tracks = []
    for proc in recording.processes:
        tracks.append(
            {
                "device_id": proc.device_id,
                "device_name": label_of(proc, devices),
                "source": source_of(proc, devices),
                "wav": proc.output_path.name,
            }
        )
    path = manifest_path(recording)
    path.write_text(
        json.dumps({"recording_id": recording.session_id, "part": recording.part, "tracks": tracks})
    )


def repair_wav(path: Path, fix: bool = True) -> float:
    """Fix the RIFF and data sizes of a WAV whose writer was killed, in place (or, with
    `fix` False, only measure it: a recorder may still be writing).

    Returns the seconds of audio in the file (0 when there is none or it is not a WAV)."""
    try:
        size = path.stat().st_size
        with path.open("r+b" if fix else "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return 0.0
            byte_rate = 0
            block_align = 1
            pos = 12
            while pos + 8 <= size:
                f.seek(pos)
                chunk_id, chunk_size = struct.unpack("<4sI", f.read(8))
                if chunk_id == b"fmt ":
                    fmt = f.read(16)
                    if len(fmt) == 16:
                        _, _, _, byte_rate, block_align, _ = struct.unpack("<HHIIHH", fmt)
                elif chunk_id == b"data":
                    start = pos + 8
                    available = size - start
                    available -= available % max(block_align, 1)
                    if fix and chunk_size != available:
                        f.seek(pos + 4)
                        f.write(struct.pack("<I", available))
                    if fix and struct.unpack("<I", head[4:8])[0] != start + available - 8:
                        f.seek(4)
                        f.write(struct.pack("<I", start + available - 8))
                    return available / byte_rate if byte_rate else 0.0
                pos += 8 + chunk_size + (chunk_size % 2)
    except OSError:
        logger.warning("Could not repair %s", path, exc_info=True)
    return 0.0


def _recorders_writing(paths: set[str]) -> list[int]:
    """pids of pw-record processes whose output is one of `paths`."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or os.path.basename(argv[0].decode(errors="replace")) != "pw-record":
            continue
        if any(a.decode(errors="replace") in paths for a in argv[1:]):
            pids.append(int(entry.name))
    return pids


def _parent(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _is_python(pid: int) -> bool:
    try:
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]
    except OSError:
        return False
    return os.path.basename(argv0.decode(errors="replace")).startswith("python")


def recorder_owner(wavs: list[Path]) -> int | None:
    """The pid of a live backend still recording to `wavs`, if any. Its recorders are its own
    business: the recording is not interrupted, just not ours. (An orphaned recorder's parent
    is init or a subreaper such as systemd --user, never Python.)"""
    for pid in _recorders_writing({str(p) for p in wavs}):
        parent = _parent(pid)
        if parent and parent != os.getpid() and _is_python(parent):
            return parent
    return None


def stop_orphan_recorders(wavs: list[Path], timeout: float = 3.0) -> int:
    """Stop pw-record processes left over from a previous backend that still write to `wavs`.
    SIGTERM lets pw-record finish its header. Returns how many were stopped."""
    pids = _recorders_writing({str(p) for p in wavs})
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    waiting = list(pids)
    deadline = time.monotonic() + timeout
    while waiting and time.monotonic() < deadline:
        time.sleep(0.1)
        waiting = [p for p in waiting if Path(f"/proc/{p}").exists()]
    for pid in waiting:  # the header is repaired afterwards anyway
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return len(pids)


def interrupted_sessions(app: AppContext) -> list[str]:
    return [
        s.id
        for s in app.sessions.list_sessions()
        if s.status in INTERRUPTED and s.id not in app.active_recordings
    ]


def _stems(session) -> set[str]:
    return {Path(r.path).name.split(".")[0] for r in session.recordings}


def pending_recordings(
    folder: Path, session
) -> list[tuple[Path | None, str | None, int | None, list[dict]]]:
    """Recordings in a meeting's folder that are not in the meeting yet: (manifest, recording
    id, part, tracks), by part. Manifests of recordings already saved are removed. Without a
    manifest (recordings before 0.8), every WAV is one, part unknown."""
    known = _stems(session)
    out = []
    for manifest in sorted(folder.glob("recording*.json")):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        tracks = data.get("tracks", [])
        if tracks and {Path(t["wav"]).stem for t in tracks} <= known:
            manifest.unlink(missing_ok=True)  # saved already: nothing to recover
            continue
        out.append((manifest, data.get("recording_id"), data.get("part"), tracks))
    if not out:
        wavs = [w for w in sorted(folder.glob("*.wav")) if w.stem not in known]
        if wavs:
            tracks = [
                {
                    "device_id": 0,
                    "device_name": f"Recovered track {i}",
                    "source": "mic",
                    "wav": w.name,
                }
                for i, w in enumerate(wavs, 1)
            ]
            out.append((None, None, None, tracks))
    return sorted(out, key=lambda m: m[2] if m[2] is not None else 1 << 30)


def pending_parts(folder: Path, session) -> set[int]:
    """Part numbers taken by recordings not saved yet (a new recording must not reuse one)."""
    if not folder.is_dir():
        return set()
    return {part for _, _, part, _ in pending_recordings(folder, session) if part is not None}


def recover_session(app: AppContext, session_id: str):
    """Job runner that finishes the interrupted recordings of a meeting (usually one)."""

    async def run(ctx: JobContext) -> dict:
        from . import history

        try:
            return await recover(ctx)
        except Exception as e:
            history.log(app, session_id, "recover_failed", error=str(e))
            raise

    async def recover(ctx: JobContext) -> dict:
        from . import history
        from .encryption import seal_session_audio
        from .parts import add_part, next_part

        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        folder = app.settings.recordings_dir / session_id
        pending = pending_recordings(folder, session) if folder.is_dir() else []
        had_transcript = bool(session.transcript)
        ctx.update(message="Recovering an interrupted recording", progress=0.0)
        seconds, parts = 0.0, []
        for manifest, recording_id, part, tracks in pending:
            session = app.sessions.get_session(session_id)
            if part is None:  # no manifest: after whatever the meeting already has
                part = next_part(session)
            wavs = [folder / t["wav"] for t in tracks]
            # When the recording really stopped: its files' last write (the history shows the
            # part then, not when it was recovered).
            written = [w.stat().st_mtime for w in wavs if w.exists()]
            ended = datetime.fromtimestamp(max(written)).isoformat() if written else None
            stopped = await asyncio.to_thread(
                stop_orphan_recorders, [w for w in wavs if w.exists()]
            )
            if stopped:
                logger.info("Stopped %d recorder(s) left running for %s", stopped, session_id)

            recordings: list[Recording] = []
            for i, (track, wav) in enumerate(zip(tracks, wavs, strict=True)):
                ctx.update(progress=i / max(len(tracks), 1))
                ogg = wav.with_suffix(".ogg")
                if wav.exists():
                    length = await asyncio.to_thread(repair_wav, wav)
                    if length <= 0:
                        continue
                    ogg = await convert_to_opus(wav)
                elif ogg.exists():  # interrupted after encoding this track
                    length = 0.0
                else:
                    continue
                seconds = max(seconds, length)
                recordings.append(
                    Recording(
                        source=track.get("source", "mic"),
                        device_id=track.get("device_id", 0),
                        device_name=track.get("device_name", "Recovered track"),
                        path=str(ogg),
                    )
                )
            if not recordings:
                history.log(app, session_id, "recover_empty", part)
                if manifest is not None:
                    manifest.unlink(missing_ok=True)
                continue
            mixed = folder / f"{recording_id or session_id}_mixed.ogg"
            mixed = await asyncio.to_thread(
                mix_audio_files, [Path(r.path) for r in recordings], mixed
            )
            part_seconds = 0.0
            for r in recordings:
                measured = await asyncio.to_thread(history.measure, Path(r.path), None)
                part_seconds = max(part_seconds, measured or 0.0)
            offset = await add_part(app, session_id, part, recordings, mixed)
            history.log(
                app,
                session_id,
                "recovered",
                recordings[0].part,
                offset=round(offset, 1),
                seconds=round(part_seconds, 1),
                sources=[r.device_name for r in recordings],
                ended=ended,
            )
            await asyncio.to_thread(seal_session_audio, app, session_id)
            if manifest is not None:
                manifest.unlink(missing_ok=True)
            parts.append(part)

        session = app.sessions.get_session(session_id)
        if not parts:
            status = SessionStatus.CREATED if session.audio_file else SessionStatus.ERROR
            app.sessions.set_status(session_id, status)
            if status == SessionStatus.ERROR:
                raise ValueError("The interrupted recording has no audio")
            return {"recovered": 0}
        app.sessions.set_status(session_id, SessionStatus.CREATED)

        transcribing = False
        if app.settings.auto_transcribe:
            from .pipeline import transcribe_session

            redo = parts if had_transcript and all(parts) else None
            app.jobs.submit("transcribe", transcribe_session(app, session_id, redo), session_id)
            transcribing = True

        done = RecoveredRecording(
            session_id=session_id, name=session.name, seconds=seconds, transcribing=transcribing
        )
        app.recovered.append(done)
        ctx.emit({"type": "recovered", **done.model_dump(mode="json")})
        return done.model_dump(mode="json")

    return run


def recover_interrupted(app: AppContext) -> list[str]:
    """Queue a recovery job for every interrupted recording. Returns the session ids."""
    ids = []
    for session_id in interrupted_sessions(app):
        folder = app.settings.recordings_dir / session_id
        session = app.sessions.get_session(session_id)
        pending = pending_recordings(folder, session) if folder.is_dir() and session else []
        owner = recorder_owner([folder / t["wav"] for _, _, _, tracks in pending for t in tracks])
        if owner is not None:
            logger.warning(
                "Session %s is still being recorded by another backend (pid %d); leaving it",
                session_id,
                owner,
            )
            continue
        logger.warning("Recording of session %s was interrupted; recovering it", session_id)
        from . import history

        for _, _, part, tracks in pending:
            history.log(app, session_id, "interrupted", part, tracks=len(tracks))
        app.jobs.submit("recover", recover_session(app, session_id), session_id)
        ids.append(session_id)
    return ids
