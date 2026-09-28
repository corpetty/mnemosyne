"""Share a quote: the text of a range of transcript lines, and that stretch of audio.

Clips are cut from the session's mixed audio with ffmpeg into the session's own folder
(`recordings/<id>/clips/`), so the storage report counts them and deleting the session or its
audio removes them.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ..models.session import Session
from ..summarization.prompts import mmss

PAD = 0.3  # seconds of audio kept before the first and after the last word
MAX_SECONDS = 600.0


def quote_text(session: Session, first: int, last: int) -> str:
    """Markdown blockquote, one line per transcript line, with speaker and time."""
    lines = [
        f"> **{seg.speaker}** [{mmss(seg.start)}]: {seg.text.strip()}"
        for seg in session.transcript[first : last + 1]
    ]
    day = session.created_at.strftime("%Y-%m-%d")
    return "\n".join(lines) + f"\n\n— *{session.name}*, {day}"


def clip_filename(session: Session, start: float) -> str:
    """A readable name for saving: the meeting's name and where the quote starts."""
    name = re.sub(r"[^\w\- ]+", "", session.name).strip() or "Quote"
    return f"{name} {int(start // 60)}m{int(start % 60):02d}s.ogg"


async def cut_clip(session: Session, clips_dir: Path, first: int, last: int) -> Path:
    """Cut [first line start - PAD, last line end + PAD] of the mixed audio to Opus."""
    if not session.audio_file or not Path(session.audio_file).exists():
        raise ValueError("This meeting has no audio")
    start = max(0.0, session.transcript[first].start - PAD)
    end = session.transcript[last].end + PAD
    if end - start > MAX_SECONDS:
        raise ValueError(f"A clip can be at most {int(MAX_SECONDS // 60)} minutes long")
    clips_dir.mkdir(parents=True, exist_ok=True)
    out = clips_dir / f"{first}-{last}.ogg"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        session.audio_file,
        "-c:a",
        "libopus",
        "-b:a",
        "64k",
        str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg could not cut the clip: {err.decode(errors='replace')[-300:]}")
    return out
