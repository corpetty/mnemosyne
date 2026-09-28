"""Meetings recorded in several parts: recording again into a meeting (after a pause, a crash or
a restart) adds to it instead of replacing it.

Each recording belongs to a part and knows where that part starts on the meeting's timeline
(`Recording.part`, `Recording.offset`). The meeting's audio is every part's mix joined end to end;
its transcript is every part's, shifted by the part's offset, with the speakers of a later part
matched to the earlier ones by voice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..models.session import Recording, Session
from ..models.transcript import TranscriptSegment
from ..storage.crypto import plaintext

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)


def next_part(session: Session) -> int:
    """The part a new recording into this meeting becomes (0 for a meeting without audio)."""
    if not session.audio_file or not session.recordings:
        return 0
    return max(r.part for r in session.recordings) + 1


def offsets(session: Session) -> dict[int, float]:
    """part -> where it starts on the meeting's timeline (seconds)."""
    out: dict[int, float] = {}
    for r in session.recordings:
        out.setdefault(r.part, r.offset)
    return out or {0: 0.0}


def part_of(start: float, starts: dict[int, float]) -> int:
    """The part a moment on the timeline falls in."""
    best = min(starts)
    for part, offset in sorted(starts.items(), key=lambda kv: kv[1]):
        if start >= offset - 0.01:
            best = part
    return best


def audio_seconds(path: str | Path, file_key: bytes | None) -> float:
    """Length of an audio file (encrypted or not)."""
    with plaintext(path, file_key) as plain:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(plain),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            return float(json.loads(out.stdout)["format"]["duration"])
        except (ValueError, KeyError, TypeError):
            from ..audio.mixer import decode_audio

            return len(decode_audio(plain, sample_rate=16000)) / 16000


def _ffmpeg(*args: str) -> None:
    out = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True, timeout=3600
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffmpeg: {out.stderr[-300:]}")


def join(first: Path, second: Path, out: Path, file_key: bytes | None) -> Path:
    """`first` then `second` as one Opus file (either may be encrypted)."""
    with plaintext(first, file_key) as a, plaintext(second, file_key) as b:
        _ffmpeg(
            "-i", str(a), "-i", str(b),
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]", "-map", "[out]",
            "-c:a", "libopus", "-b:a", "64k", str(out),
        )  # fmt: skip
    return out


def cut(source: Path, start: float, end: float | None, out: Path, file_key: bytes | None) -> Path:
    """[start, end) of an audio file (end None: to the end), as Opus."""
    span = ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end is not None else [])
    with plaintext(source, file_key) as src:
        _ffmpeg(*span, "-i", str(src), "-c:a", "libopus", "-b:a", "64k", str(out))
    return out


async def add_part(
    app: AppContext, session_id: str, part: int, recordings: list[Recording], part_mix: Path
) -> float:
    """Attach a finished recording to its meeting. Part 0 becomes the meeting's audio; a later
    part is joined after what is there. Returns the part's offset on the timeline."""
    session = app.sessions.get_session(session_id)
    existing = Path(session.audio_file) if session and session.audio_file else None
    if part == 0 or existing is None or not existing.is_file():
        for r in recordings:
            r.part, r.offset = 0, 0.0
        keep = [r for r in session.recordings if r.part != 0] if session else []
        app.sessions.set_audio(session_id, str(part_mix), keep + recordings)
        return 0.0
    offset = await asyncio.to_thread(audio_seconds, existing, app.file_key)
    for r in recordings:
        r.part, r.offset = part, offset
    combined = part_mix.with_name(f"meeting_{part}.ogg")
    # Into a new file, then renamed: never read and write the meeting's audio at once.
    joining = part_mix.with_name(f".joining-{part}.ogg")
    await asyncio.to_thread(join, existing, part_mix, joining, app.file_key)
    joining.replace(combined)
    app.sessions.set_audio(session_id, str(combined), session.recordings + recordings)
    referenced = {r.path for r in session.recordings} | {str(combined)}
    for old in (existing, part_mix):
        if str(old) not in referenced:
            old.unlink(missing_ok=True)
    logger.info("Session %s: part %d added at %.1f s", session_id, part, offset)
    return offset


def match_speakers(
    new: dict[str, list[float]],
    known: dict[str, list[float]],
    taken: set[str],
    threshold: float,
) -> dict[str, str]:
    """Labels of a later part -> labels already in the meeting, by voice: best matches first,
    one to one, above `threshold` (cosine). Others get the next free SPEAKER_nn."""

    def unit(v):
        a = np.asarray(v, dtype=np.float32)
        return a / (np.linalg.norm(a) or 1.0)

    scored = sorted(
        (
            (float(np.dot(unit(v), unit(k))), label, known_label)
            for label, v in new.items()
            for known_label, k in known.items()
        ),
        reverse=True,
    )
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for score, label, known_label in scored:
        if score < threshold:
            break
        if label in mapping or known_label in used:
            continue
        mapping[label] = known_label
        used.add(known_label)
    numbers = [
        int(t[len("SPEAKER_") :])
        for t in taken | set(known)
        if t.startswith("SPEAKER_") and t[len("SPEAKER_") :].isdigit()
    ]
    n = max(numbers, default=-1) + 1
    for label in sorted(new):
        if label not in mapping:
            mapping[label] = f"SPEAKER_{n:02d}"
            n += 1
    return mapping


def shift(segments: list[TranscriptSegment], offset: float) -> list[TranscriptSegment]:
    if not offset:
        return segments
    out = []
    for s in segments:
        words = (
            [
                w.model_copy(update={"start": w.start + offset, "end": w.end + offset})
                for w in s.words
            ]
            if s.words
            else s.words
        )
        out.append(
            s.model_copy(update={"start": s.start + offset, "end": s.end + offset, "words": words})
        )
    return out
