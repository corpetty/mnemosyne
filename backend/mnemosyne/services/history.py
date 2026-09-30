"""A meeting's history: the parts it is made of (when each was recorded, how long, the gaps
between them, the files behind them), audio recorded but not in it yet, and a log of what
happened to it (recordings started and stopped and why, parts saved or recovered, capture
problems, imports, combining, transcriptions, summaries).

The log starts with 0.10.2; for older meetings the parts are worked out from their recordings.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..models.history import (
    HistoryEvent,
    HistoryFile,
    HistoryPart,
    PendingRecording,
    SessionHistory,
)

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".ogg", ".opus", ".wav", ".mp3", ".m4a", ".flac", ".webm", ".mp4", ".enc"}
# Where a part came from, by the event that saved it.
HOW = {"part_saved": "recorded", "recovered": "recovered", "imported": "imported"}
_lengths: dict[tuple[str, int, float], float] = {}  # (path, size, mtime) -> seconds


def log(app: AppContext, session_id: str, kind: str, part: int | None = None, **detail) -> None:
    """Add to a meeting's history. Never raises: the log must not break what it records."""
    try:
        app.repo.add_event(session_id, kind, part, detail)
        app.bus.publish({"type": "history", "session_id": session_id})
    except Exception:
        logger.warning("Could not log %s for session %s", kind, session_id, exc_info=True)


ACCESS_EVERY = 1800.0  # one "viewed" per person and meeting per half hour, not per request
_accessed: dict[tuple[str, str, str], float] = {}


def log_access(app: AppContext, session_id: str, kind: str) -> None:
    """On a firm's server, note who opened, played or exported a meeting (access.py), so "who
    looked at this client's meeting" has an answer. Nothing for the desktop app or the admin
    token. Never raises."""
    import time

    from .. import access

    who = access.principal()
    if who is None:
        return
    key = (who.user_id, session_id, kind)
    now = time.monotonic()
    if now - _accessed.get(key, -ACCESS_EVERY) < ACCESS_EVERY:
        return
    _accessed[key] = now
    log(app, session_id, kind, by=who.name, user_id=who.user_id, role=who.role)


def measure(path: Path, file_key: bytes | None) -> float | None:
    """An audio file's length, remembered while the file is unchanged."""
    from .parts import audio_seconds

    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _lengths:
        try:
            _lengths[key] = audio_seconds(path, file_key)
        except Exception:
            logger.debug("Could not measure %s", path, exc_info=True)
            return None
    return _lengths[key]


def _file(path: Path, source: str = "", device_name: str = "") -> HistoryFile:
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return HistoryFile(name=path.name, source=source, device_name=device_name, size=size)


def build(app: AppContext, session_id: str) -> SessionHistory | None:
    """The history of a meeting (blocking: it reads the disk; run it in a thread)."""
    from .recovery import pending_recordings, repair_wav

    session = app.sessions.get_session(session_id)
    if session is None:
        return None
    folder = app.settings.recordings_dir / session_id
    events = [HistoryEvent(**e) for e in app.repo.events(session_id)]
    if not any(e.kind == "created" for e in events):
        events.insert(0, HistoryEvent(at=session.created_at, kind="created"))

    # ---- parts, from the recordings (every meeting has them) and the log (newer ones) ----
    by_part: dict[int, list] = {}
    for r in session.recordings:
        by_part.setdefault(r.part, []).append(r)
    last_event: dict[tuple[str, int], HistoryEvent] = {}
    for e in events:
        if e.part is not None:
            last_event[(e.kind, e.part)] = e
    combined = {n for e in events if e.kind == "combined" for n in e.detail.get("parts", [])}
    numbers = sorted(by_part)
    parts: list[HistoryPart] = []
    for i, n in enumerate(numbers):
        recs = by_part[n]
        offset = min(r.offset for r in recs)
        saved = next(
            (
                last_event[(k, n)]
                for k in ("part_saved", "recovered", "imported")
                if (k, n) in last_event
            ),
            None,
        )
        seconds = saved.detail.get("seconds") if saved else None
        if seconds is None and i + 1 < len(numbers):
            seconds = min(r.offset for r in by_part[numbers[i + 1]]) - offset
        if seconds is None:  # the last part: measure its first source that is there
            for r in recs:
                if (seconds := measure(Path(r.path), app.file_key)) is not None:
                    break
        started = last_event.get(("recording_started", n))
        saved_at = saved.at if saved else max(r.created_at for r in recs)
        # A recovered part ended when its files were last written, not when it was recovered.
        ended_iso = saved.detail.get("ended") if saved and saved.kind == "recovered" else None
        if started is not None and (saved is None or started.at <= saved.at):
            started_at, approximate = started.at, False
        elif ended_iso and seconds is not None:
            started_at = datetime.fromisoformat(ended_iso) - timedelta(seconds=seconds)
            approximate = False
        elif seconds is not None and (saved is None or saved.kind != "imported"):
            # Saved a few seconds after it stopped: close enough to see the gaps.
            started_at, approximate = saved_at - timedelta(seconds=seconds), True
        else:
            started_at, approximate = None, True
        stopped = last_event.get(("recording_stopped", n))
        if stopped is not None and started is not None and not approximate:
            ended_at = stopped.at
        elif ended_iso and started is None:
            ended_at = datetime.fromisoformat(ended_iso)
        elif started_at is not None and seconds is not None:
            ended_at = started_at + timedelta(seconds=seconds)
        else:
            ended_at = None
        how = HOW.get(saved.kind, "recorded") if saved else "recorded"
        if all(r.source == "import" for r in recs):
            how = "imported"
        elif n in combined:
            how = "combined"
        parts.append(
            HistoryPart(
                part=n,
                offset=offset,
                seconds=seconds,
                started_at=started_at,
                ended_at=ended_at,
                approximate=approximate,
                gap_before=None,
                how=how,
                files=[_file(Path(r.path), r.source, r.device_name) for r in recs],
            )
        )

    # ---- the recording in progress, if any ----
    active = app.active_recordings.get(session_id)
    live_wavs: set[str] = set()
    if active is not None:
        live_wavs = {p.output_path.name for p in active.processes}
        began = datetime.fromtimestamp(active.started_at)
        parts.append(
            HistoryPart(
                part=active.part,
                offset=parts[-1].offset + (parts[-1].seconds or 0) if parts else 0.0,
                seconds=max(0.0, time.time() - active.started_at),
                started_at=began,
                ended_at=None,
                approximate=False,
                gap_before=None,
                how="recording",
                files=[
                    _file(p.output_path, "", active.labels.get(p.device_id, str(p.device_id)))
                    for p in active.processes
                ],
            )
        )

    # Gaps: wall-clock time between one part's end and the next one's start. An imported
    # file has no place on the clock.
    for prev, cur in zip(parts, parts[1:], strict=False):
        if "imported" in (prev.how, cur.how):
            continue
        if prev.ended_at and cur.started_at:
            gap = (cur.started_at - prev.ended_at).total_seconds()
            cur.gap_before = gap if gap > 1 else 0.0

    # ---- audio on disk that is not in the meeting ----
    busy = any(
        j.kind in ("finish", "recover")
        for j in app.jobs.list(session_id=session_id, active_only=True)
    )
    pending: list[PendingRecording] = []
    if folder.is_dir():
        for _manifest, recording_id, part, tracks in pending_recordings(folder, session):
            if active is not None and (
                recording_id == active.session_id or {t["wav"] for t in tracks} <= live_wavs
            ):
                continue  # the recording in progress
            files = []
            longest = 0.0
            for t in tracks:
                wav = folder / t["wav"]
                f = _file(wav, t.get("source", ""), t.get("device_name", ""))
                if wav.exists():
                    f.seconds = repair_wav(wav, fix=False)
                    longest = max(longest, f.seconds)
                elif (ogg := wav.with_suffix(".ogg")).exists():
                    f = _file(ogg, t.get("source", ""), t.get("device_name", ""))
                files.append(f)
            pending.append(
                PendingRecording(
                    recording_id=recording_id,
                    part=part,
                    seconds=longest,
                    state="saving" if busy else "waiting",
                    files=files,
                )
            )

    referenced = {Path(r.path).name for r in session.recordings}
    if session.audio_file:
        referenced.add(Path(session.audio_file).name)
    referenced |= live_wavs
    for p in pending:
        referenced |= {f.name for f in p.files}
        referenced |= {Path(f.name).with_suffix(".ogg").name for f in p.files}
    missing = sorted(
        Path(path).name
        for path in [*(r.path for r in session.recordings), session.audio_file]
        if path and not Path(path).exists()
    )
    orphans = []
    if folder.is_dir() and not busy:  # a save in progress has files in flight
        for f in sorted(folder.iterdir()):
            if (
                f.is_file()
                and not f.name.startswith(".")
                and (f.suffix in AUDIO_SUFFIXES)
                and f.name not in referenced
            ):
                orphans.append(_file(f))

    recorded = sum(p.seconds or 0 for p in parts)
    gaps = sum(p.gap_before or 0 for p in parts)
    return SessionHistory(
        session_id=session_id,
        recording_now=active is not None,
        parts=parts,
        recorded_seconds=recorded,
        gap_seconds=gaps,
        pending=pending,
        missing=missing,
        orphans=orphans,
        events=events,
    )
