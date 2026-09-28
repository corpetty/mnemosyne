"""Transcript data models."""

from .base import ApiModel


class WordSegment(ApiModel):
    word: str
    start: float
    end: float
    score: float = 0.0


class TranscriptSegment(ApiModel):
    text: str
    speaker: str
    start: float
    end: float
    words: list[WordSegment] | None = None


def line_at(transcript: list[TranscriptSegment], at: float) -> TranscriptSegment | None:
    """The line being said at a moment: the last one to start by then (or the first)."""
    said = [s for s in transcript if s.start <= at + 0.5]
    return said[-1] if said else (transcript[0] if transcript else None)
