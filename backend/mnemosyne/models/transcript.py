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
