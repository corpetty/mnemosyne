"""Search result models."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import ApiModel


class SegmentHit(ApiModel):
    idx: int
    speaker: str
    start: float
    snippet: str  # matches wrapped in [[ ]]
    semantic: bool = False  # found by meaning, not by the words typed


class CommentHit(ApiModel):
    id: str
    author: str
    target: str  # segment / body / decision / question / item, '' = the meeting
    target_id: str
    snippet: str  # matches wrapped in [[ ]]


class SearchHit(ApiModel):
    session_id: str
    session_name: str
    created_at: datetime
    score: float
    session_snippet: str | None = None  # match in name/summary/notes
    segments: list[SegmentHit] = Field(default_factory=list)
    comments: list[CommentHit] = Field(default_factory=list)  # kind "comment" hits
    match: Literal["keyword", "semantic", "both"] = "keyword"
