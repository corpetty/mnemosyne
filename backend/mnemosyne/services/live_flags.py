"""Live lines marked "Not said" (routes/live_flags.py), applied to a final transcription.

A flag is a span of the live transcript on one source ("mic" or "system"), in seconds into
the recording (part) it was heard in. The final transcript of that part is on the same
timeline (it comes from the part's own files, before `shift` puts it on the meeting's), so a
final line goes when it comes from the flag's source and at least half of its own duration
lies inside the flag's span. The flags stay: a later transcription drops the lines again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models.live_flag import LiveFlag
from ..models.transcript import TranscriptSegment

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

MIN_OVERLAP = 0.5  # of the final line's own duration


def covered(seg: TranscriptSegment, flag: LiveFlag) -> bool:
    """Does the flag's span cover at least half of the segment's own duration?"""
    duration = seg.end - seg.start
    if duration <= 0:  # a point in time: inside the span
        return flag.start <= seg.start <= flag.end
    overlap = min(seg.end, flag.end) - max(seg.start, flag.start)
    return overlap >= MIN_OVERLAP * duration


def drop_flagged(
    segments: list[TranscriptSegment],
    kinds: dict[int, str],
    flags: list[LiveFlag],
) -> tuple[list[TranscriptSegment], int]:
    """(segments without the flagged ones, how many went). `kinds` maps id(segment) to the
    source it came from. A segment of the "mixed" file (one file for every source) is
    matched against flags of any source; one whose source is not known is kept."""
    if not flags:
        return segments, 0
    kept = []
    for seg in segments:
        kind = kinds.get(id(seg))
        hit = kind is not None and any(
            (kind == "mixed" or f.source == kind) and covered(seg, f) for f in flags
        )
        if not hit:
            kept.append(seg)
    return kept, len(segments) - len(kept)


def apply(
    app: AppContext,
    session_id: str,
    part: int,
    segments: list[TranscriptSegment],
    engine,
) -> tuple[list[TranscriptSegment], int]:
    """The segments one part's transcription gave, without the lines marked "Not said"
    while that part was recorded. Never raises: the transcription must not fail for this."""
    try:
        flags = [f for f in app.repo.list_live_flags(session_id) if f.part == part]
        if not flags:
            return segments, 0
        kinds = {id(seg): kind for seg, kind in getattr(engine, "last_segment_sources", [])}
        return drop_flagged(segments, kinds, flags)
    except Exception:
        logger.warning("Could not apply the lines marked Not said to %s", session_id, exc_info=True)
        return segments, 0
