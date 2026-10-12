"""A live transcript line marked "Not said" while recording (services/live_flags.py)."""

from datetime import datetime

from pydantic import Field

from .base import ApiModel


class LiveFlag(ApiModel):
    id: str
    source: str  # the live source kind: "mic" or "system"
    start: float  # seconds into the part's recording, as the live_segment event had them
    end: float
    text: str
    part: int = 0  # the recording (part) the line was heard in
    created_at: datetime = Field(default_factory=datetime.now)


class LiveFlagCreate(ApiModel):
    source: str
    start: float
    end: float
    text: str = ""
    # Which part the line is in; left out, the part being recorded now.
    part: int | None = None
