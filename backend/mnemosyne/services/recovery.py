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

MANIFEST = "recording.json"
INTERRUPTED = (SessionStatus.RECORDING, SessionStatus.ENCODING)


class RecoveredRecording(ApiModel):
    session_id: str
    name: str
    seconds: float
    transcribing: bool


def write_manifest(recording: RecordingSession, devices: dict[int, AudioDevice]) -> None:
    """Remember which file is which source. Device ids change after a reboot, so this cannot
    be looked up at recovery time."""
    tracks = []
    for proc in recording.processes:
        device = devices.get(proc.device_id)
        tracks.append(
            {
                "device_id": proc.device_id,
                "device_name": device.description if device else str(proc.device_id),
                "source": "system" if (device is not None and device.is_output) else "mic",
                "wav": proc.output_path.name,
            }
        )
    path = recording.output_dir / MANIFEST
    path.write_text(json.dumps({"recording_id": recording.session_id, "tracks": tracks}))


def repair_wav(path: Path) -> float:
    """Fix the RIFF and data sizes of a WAV whose writer was killed, in place.

    Returns the seconds of audio in the file (0 when there is none or it is not a WAV)."""
    try:
        size = path.stat().st_size
        with path.open("r+b") as f:
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
                    if chunk_size != available:
                        f.seek(pos + 4)
                        f.write(struct.pack("<I", available))
                    if struct.unpack("<I", head[4:8])[0] != start + available - 8:
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


def _tracks(folder: Path) -> tuple[str | None, list[dict]]:
    """The recording's tracks: from the manifest, or every WAV (recordings before 0.8)."""
    try:
        manifest = json.loads((folder / MANIFEST).read_text())
        return manifest.get("recording_id"), manifest.get("tracks", [])
    except (OSError, ValueError):
        pass
    wavs = sorted(folder.glob("*.wav"))
    return None, [
        {"device_id": 0, "device_name": f"Recovered track {i}", "source": "mic", "wav": w.name}
        for i, w in enumerate(wavs, 1)
    ]


def recover_session(app: AppContext, session_id: str):
    """Job runner that finishes an interrupted recording."""

    async def run(ctx: JobContext) -> dict:
        session = app.sessions.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        folder = app.settings.recordings_dir / session_id
        recording_id, tracks = _tracks(folder)
        ctx.update(message="Recovering an interrupted recording", progress=0.0)

        wavs = [folder / t["wav"] for t in tracks]
        stopped = await asyncio.to_thread(stop_orphan_recorders, [w for w in wavs if w.exists()])
        if stopped:
            logger.info("Stopped %d recorder(s) left running for %s", stopped, session_id)

        recordings: list[Recording] = []
        seconds = 0.0
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
            status = SessionStatus.CREATED if session.audio_file else SessionStatus.ERROR
            app.sessions.set_status(session_id, status)
            raise ValueError("The interrupted recording has no audio")

        mixed = folder / f"{recording_id or session_id}_mixed.ogg"
        mixed = await asyncio.to_thread(mix_audio_files, [Path(r.path) for r in recordings], mixed)
        app.sessions.set_audio(session_id, str(mixed), recordings)
        app.sessions.set_status(session_id, SessionStatus.CREATED)

        transcribing = False
        if app.settings.auto_transcribe:
            from .pipeline import transcribe_session

            app.jobs.submit("transcribe", transcribe_session(app, session_id), session_id)
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
    ids = interrupted_sessions(app)
    for session_id in ids:
        logger.warning("Recording of session %s was interrupted; recovering it", session_id)
        app.jobs.submit("recover", recover_session(app, session_id), session_id)
    return ids
