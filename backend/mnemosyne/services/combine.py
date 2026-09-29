"""Combine two meetings into one: a call that dropped and was recorded again as a new meeting,
or a meeting split in two by mistake.

Every part of both meetings (services/parts.py) is put in the order it was recorded and joined
into one meeting audio; transcripts move with their parts. The other meeting's speakers are
matched to this one's by voice; a name given there and not matched here stays. Its files move
into this meeting's folder (nothing is re-encoded but the joined audio), its notes are added to
these, and it is then deleted. A summary is made again over the whole meeting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..models.session import Recording, Session, SessionStatus
from ..models.transcript import TranscriptSegment
from .parts import audio_seconds, cut, join, match_speakers, offsets, part_of, shift

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


@dataclass
class Piece:
    """One part of a meeting, cut out of it."""

    session: Session
    part: int  # its part number in its meeting
    at: datetime  # when it was recorded
    length: float
    audio: Path  # its audio (the meeting's own file, or a private cut)
    recordings: list[Recording]
    segments: list[TranscriptSegment]  # times from the start of the piece


def _generic(label: str) -> bool:
    return label.startswith("SPEAKER_") or label == "UNKNOWN"


def pieces(app: AppContext, session: Session, stack: contextlib.ExitStack) -> list[Piece]:
    from ..storage.crypto import _scratch_dir

    total = audio_seconds(session.audio_file, app.file_key)
    starts = sorted(offsets(session).items(), key=lambda kv: kv[1])
    by_part = dict(starts)
    out = []
    for i, (part, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else None
        if len(starts) == 1:
            audio = Path(session.audio_file)
        else:
            audio = _scratch_dir() / f"{session.id}-combine-{part}.ogg"
            stack.callback(audio.unlink, missing_ok=True)
            cut(Path(session.audio_file), start, end, audio, app.file_key)
        recordings = [r for r in session.recordings if r.part == part]
        out.append(
            Piece(
                session=session,
                part=part,
                at=min((r.created_at for r in recordings), default=session.created_at),
                length=(end if end is not None else total) - start,
                audio=audio,
                recordings=recordings,
                segments=shift(
                    [s for s in session.transcript if part_of(s.start, by_part) == part], -start
                ),
            )
        )
    return out


def relabel_speakers(
    other: Session,
    other_embeddings: dict[str, list[float]],
    target: Session,
    target_embeddings: dict[str, list[float]],
    threshold: float,
) -> dict[str, str]:
    """The other meeting's speaker labels -> labels in this one."""
    taken = {s.speaker for s in target.transcript} | set(target_embeddings)
    relabel = match_speakers(other_embeddings, target_embeddings, taken, threshold)
    for label, new in list(relabel.items()):
        if not _generic(label) and _generic(new):  # a name given there, no voice match here
            relabel[label] = label
    used = taken | set(relabel.values())
    numbers = [int(x[8:]) for x in used if x.startswith("SPEAKER_") and x[8:].isdigit()]
    n = max(numbers, default=-1) + 1
    for label in sorted({s.speaker for s in other.transcript} - set(relabel)):
        if label.startswith("SPEAKER_"):
            relabel[label] = f"SPEAKER_{n:02d}"
            n += 1
        else:
            relabel[label] = label
    return relabel


def _move(path: Path, folder: Path, prefix: str) -> Path:
    if not path.exists() or path.parent == folder:
        return path
    target = folder / f"{prefix}_{path.name}"
    path.rename(target)
    return target


def combine_runner(app: AppContext, target_id: str, other_id: str):
    """Job runner: `other_id` joins `target_id`, which keeps its name and id."""

    async def run(ctx) -> dict:
        target = app.sessions.get_session(target_id)
        other = app.sessions.get_session(other_id)
        if target is None or other is None:
            raise ValueError("Meeting not found")
        before = {target_id: target.status, other_id: other.status}
        for sid in before:
            app.sessions.set_status(sid, SessionStatus.ENCODING)
        loop = asyncio.get_running_loop()

        def update(message=None, progress=None):  # from the worker thread
            loop.call_soon_threadsafe(ctx.update, message, progress)

        try:
            result = await asyncio.to_thread(_combine, app, target, other, update)
        except Exception:
            for sid, status in before.items():
                if app.sessions.get_session(sid) is not None:
                    app.sessions.set_status(sid, status)
            raise
        if target.summary or other.summary:
            from .pipeline import summarize_session

            job = app.jobs.submit("summarize", summarize_session(app, target_id), target_id)
            result["summarize_job_id"] = job.id
        return result

    return run


def _combine(app: AppContext, target: Session, other: Session, update) -> dict:
    from .encryption import seal_session_audio

    folder = app.settings.recordings_dir / target.id
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        update("Cutting the meetings into their parts", 0.05)
        all_pieces = pieces(app, target, stack) + pieces(app, other, stack)
        all_pieces.sort(key=lambda p: (p.at, p.session.id != target.id))
        relabel = relabel_speakers(
            other,
            app.repo.get_session_embeddings(other.id),
            target,
            app.repo.get_session_embeddings(target.id),
            app.settings.speaker_match_threshold,
        )

        update("Joining the audio", 0.3)
        joined = all_pieces[0].audio
        for i, piece in enumerate(all_pieces[1:], 1):
            out = folder / f".combine-{i}.ogg"
            join(joined, piece.audio, out, app.file_key)
            if joined.name.startswith(".combine-"):
                joined.unlink(missing_ok=True)
            joined = out
            update(None, 0.3 + 0.5 * i / len(all_pieces))
        final = folder / f"meeting_{len(all_pieces) - 1}.ogg"

    update("Saving", 0.85)
    old_audio = [Path(s.audio_file) for s in (target, other) if s.audio_file]
    if joined != final:
        if joined.parent == folder and joined.name.startswith(".combine-"):
            joined.replace(final)
        else:  # a single piece: impossible with two meetings, but keep its file intact
            shutil.copyfile(joined, final)
    recordings: list[Recording] = []
    segments: list[TranscriptSegment] = []
    offset = 0.0
    for part, piece in enumerate(all_pieces):
        mine = piece.session.id == target.id
        for r in piece.recordings:
            path = Path(r.path) if mine else _move(Path(r.path), folder, other.id)
            recordings.append(
                r.model_copy(update={"part": part, "offset": offset, "path": str(path)})
            )
        segs = piece.segments
        if not mine:
            segs = [
                s.model_copy(update={"speaker": relabel.get(s.speaker, s.speaker)}) for s in segs
            ]
        segments += shift(segs, offset)
        offset += piece.length
    segments.sort(key=lambda s: (s.start, s.end))

    # The other meeting's clips and anything else in its folder come along; nothing is lost.
    other_folder = app.settings.recordings_dir / other.id
    if other_folder.is_dir():
        for f in list(other_folder.rglob("*")):
            if f.is_file() and str(f) not in {r.path for r in recordings}:
                dest = folder / f.relative_to(other_folder)
                if f.parent.name != "clips":
                    dest = folder / f"from-{other.id}" / f.relative_to(other_folder)
                if f.resolve() not in {a.resolve() for a in old_audio}:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    f.rename(dest)

    referenced = {r.path for r in recordings} | {str(final)}
    for audio in old_audio:
        if str(audio) not in referenced:
            audio.unlink(missing_ok=True)

    app.sessions.set_audio(target.id, str(final), recordings)
    # Bookmarks follow their parts, which are numbered anew (before the other meeting goes).
    app.repo.move_bookmarks(
        [(piece.session.id, piece.part, target.id, n) for n, piece in enumerate(all_pieces)]
    )
    if segments:
        app.sessions.set_transcript(target.id, segments)
    embeddings = dict(app.repo.get_session_embeddings(target.id))
    for label, vector in app.repo.get_session_embeddings(other.id).items():
        embeddings.setdefault(relabel.get(label, label), vector)
    if embeddings:
        app.repo.set_session_embeddings(target.id, embeddings)
    if other.notes and other.notes.strip():
        notes = (target.notes or "").rstrip()
        add = f"## From “{other.name}”\n\n{other.notes.strip()}"
        app.sessions.update_notes(target.id, f"{notes}\n\n{add}" if notes else add)
    fields = {"attendees": list(dict.fromkeys([*target.attendees, *other.attendees]))}
    if other.local_only:  # the stricter privacy wins
        fields["local_only"] = True
    app.repo.update_fields(target.id, **fields)
    seal_session_audio(app, target.id)
    app.sessions.set_status(
        target.id, SessionStatus.COMPLETED if segments else SessionStatus.CREATED
    )
    # Its history comes along too, parts numbered as they are now.
    app.repo.move_events(
        [(piece.session.id, piece.part, target.id, n) for n, piece in enumerate(all_pieces)]
    )
    from . import history

    history.log(
        app,
        target.id,
        "combined",
        other_id=other.id,
        other_name=other.name,
        parts=[n for n, piece in enumerate(all_pieces) if piece.session.id == other.id],
    )
    app.sessions.delete_session(other.id)
    logger.info("Meeting %s combined into %s (%d parts)", other.id, target.id, len(all_pieces))
    return {"parts": len(all_pieces), "seconds": round(offset, 1), "removed": other.id}
