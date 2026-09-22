"""Search result models."""

from datetime import datetime

from pydantic import BaseModel, Field


class SegmentHit(BaseModel):
    idx: int
    speaker: str
    start: float
    snippet: str  # matches wrapped in [[ ]]


class SearchHit(BaseModel):
    session_id: str
    session_name: str
    created_at: datetime
    score: float
    session_snippet: str | None = None  # match in name/summary/notes
    segments: list[SegmentHit] = Field(default_factory=list)
