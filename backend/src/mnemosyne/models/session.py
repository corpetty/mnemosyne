"""Session data models."""

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field

from .transcript import TranscriptSegment


class SessionStatus(StrEnum):
    CREATED = "created"
    RECORDING = "recording"
    ENCODING = "encoding"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    ERROR = "error"


RecordingSource = Literal["mic", "system", "import"]

DEFAULT_SESSION_NAME = "Untitled Session"


class Recording(BaseModel):
    """One captured audio source. Sources are kept separate on disk so a later
    stage can attribute the mic channel to the local user and diarize only the rest."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    source: RecordingSource
    device_id: int
    device_name: str
    path: str
    created_at: datetime = Field(default_factory=datetime.now)


def transcript_hash(segments: list[TranscriptSegment]) -> str:
    h = hashlib.sha1()
    for seg in segments:
        h.update(f"{seg.speaker}\x1f{seg.text}\x1e".encode())
    return h.hexdigest()[:16]


class ActionItem(BaseModel):
    text: str
    owner: str | None = None


class SummaryData(BaseModel):
    """Structured summary produced by the LLM. `summary` on the session keeps
    the markdown body for compatibility."""

    title: str = ""  # short name suggested by the model; used to name untitled sessions
    # Fingerprint of the transcript (speakers + text) the summary was made from.
    source_hash: str = ""
    style: str = "meeting"
    provider: str = ""
    model: str = ""
    topics: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    name: str = DEFAULT_SESSION_NAME
    status: SessionStatus = SessionStatus.CREATED
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    audio_file: str | None = None  # mixed file used for transcription
    recordings: list[Recording] = Field(default_factory=list)
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    summary: str = ""
    summary_data: SummaryData | None = None
    notes: str = ""
    participants: list[str] = Field(default_factory=list)
    # Invitees from the calendar event this session was recorded during (names).
    attendees: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary_stale(self) -> bool:
        """True when the transcript changed (text or speakers) after it was summarized."""
        if not self.summary or self.summary_data is None or not self.summary_data.source_hash:
            return False
        return self.summary_data.source_hash != transcript_hash(self.transcript)


class SessionSummary(BaseModel):
    """Sidebar view of a session. Never carries the transcript."""

    id: str
    name: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    has_transcript: bool
    has_summary: bool
    has_audio: bool = False
    participant_count: int
