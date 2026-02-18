"""Transcript data models."""

from pydantic import BaseModel


class WordSegment(BaseModel):
    word: str
    start: float
    end: float
    score: float = 0.0


class TranscriptSegment(BaseModel):
    text: str
    speaker: str
    start: float
    end: float
    words: list[WordSegment] | None = None
