"""Known-voice profiles used to auto-label diarized speakers across sessions."""

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class SpeakerProfile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    name: str
    embedding: list[float]
    sample_count: int = 1
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class SpeakerProfileSummary(BaseModel):
    """API view without the vector."""

    id: str
    name: str
    sample_count: int
    created_at: datetime
    updated_at: datetime
