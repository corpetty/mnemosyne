"""Known-voice profiles used to auto-label diarized speakers across sessions."""

from datetime import datetime
from uuid import uuid4

from pydantic import Field

from .base import ApiModel

# Voice spaces: the model family that made a stored voice vector. Vectors are only ever compared
# inside one space (docs/plans/2026-11-17-speakers-without-torch.md). Every voice stored before
# spaces existed came from pyannote's embedding model.
PYANNOTE_SPACE = "pyannote"
WESPEAKER_SPACE = "wespeaker-r34"


class SpeakerProfile(ApiModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    name: str
    # One running-mean voice per space (space -> vector), and how many samples fed each.
    voices: dict[str, list[float]] = Field(default_factory=dict)
    sample_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def sample_count(self) -> int:
        """Samples across every space (what the API view shows)."""
        return sum(self.sample_counts.values())

    @property
    def has_voice(self) -> bool:
        return any(self.voices.values())


class SpeakerProfileSummary(ApiModel):
    """API view without the vector."""

    id: str
    name: str
    sample_count: int
    created_at: datetime
    updated_at: datetime
