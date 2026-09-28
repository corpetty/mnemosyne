"""Session data models."""

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import Field, computed_field

from .base import ApiModel
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


class Recording(ApiModel):
    """One captured audio source. Sources are kept separate on disk so a later
    stage can attribute the mic channel to the local user and diarize only the rest."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    source: RecordingSource
    device_id: int
    device_name: str
    path: str
    created_at: datetime = Field(default_factory=datetime.now)
    # Recording again into a meeting adds a part (services/parts.py): which one, and where it
    # starts on the meeting's timeline (seconds).
    part: int = 0
    offset: float = 0.0


def transcript_hash(segments: list[TranscriptSegment]) -> str:
    h = hashlib.sha1()
    for seg in segments:
        h.update(f"{seg.speaker}\x1f{seg.text}\x1e".encode())
    return h.hexdigest()[:16]


class ActionItem(ApiModel):
    text: str
    owner: str | None = None
    issue_url: str | None = None  # set once an issue was created for this item
    done: bool = False
    live: bool = False  # noted by the live copilot; the final summary did not list it
    at: float | None = None  # seconds: the transcript line where it came up
    due: date | None = None  # a deadline named in the meeting, resolved to a date


class CopilotItem(ApiModel):
    text: str
    owner: str | None = None


class CopilotNotes(ApiModel):
    """The live copilot's running notes (services/copilot.py), kept with the session."""

    session_id: str
    summary: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    action_items: list[CopilotItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    agenda_covered: list[int] = Field(default_factory=list)  # agenda points (1-based) discussed
    lines: int = 0  # transcript lines covered
    updated_at: datetime = Field(default_factory=datetime.now)


class Chapter(ApiModel):
    start: float  # seconds, snapped to the start of a transcript line
    title: str


class SummaryData(ApiModel):
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
    # Where each decision / open question came up (seconds, a line start, or None), in the
    # same order as the lists above. Separate lists so `decisions` stays plain text.
    decision_at: list[float | None] = Field(default_factory=list)
    question_at: list[float | None] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    followup: str = ""  # last drafted follow-up message (email or chat)


class Asset(ApiModel):
    """A resource for meetings: a link, or a file kept by Mnemosyne. Assets live in a library;
    a meeting lists the ones attached to it, and one can be attached to several meetings."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    kind: str  # "link" | "file"
    title: str
    url: str | None = None  # a link's address
    filename: str | None = None  # a file's name
    size: int | None = None  # a file's size in bytes
    created_at: datetime = Field(default_factory=datetime.now)
    source: str = "manual"  # "manual" | "calendar" (a link in the calendar event)
    has_text: bool = False  # text was read from the file (it goes to the summary)


class ExternalNotes(ApiModel):
    """Notes another assistant wrote about the meeting (Gemini in Google Meet, Zoom AI
    Companion, Otter, Teams Copilot...). The summary uses them to fill gaps; the transcript
    wins. A meeting with notes and no recording is summarized from the notes."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    source: str  # "Gemini", "Zoom", "Otter", "Teams Copilot", ... or "Other"
    text: str
    filename: str | None = None
    added_at: datetime = Field(default_factory=datetime.now)


class AgendaItem(ApiModel):
    """A point to get through in a meeting; the copilot marks it covered once it came up."""

    text: str
    covered: bool = False


class Bookmark(ApiModel):
    """A moment marked as important, while recording (a shortcut, the tray, a button) or later
    on a transcript line. The summary gives these moments weight."""

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    at: float  # seconds on the meeting's timeline
    note: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class Session(ApiModel):
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
    local_only: bool = False  # never sent to a cloud LLM provider
    copilot_notes: CopilotNotes | None = None  # last notes taken live while recording
    speakers_reviewed: bool = False  # the "who is who" card was completed or dismissed
    bookmarks: list[Bookmark] = Field(default_factory=list)  # in time order
    agenda: list[AgendaItem] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)  # resources attached, in order added
    external_notes: list[ExternalNotes] = Field(default_factory=list)  # other assistants' notes
    # Its kind (settings.meeting_types, by name); "" = none yet, "none" = chosen none.
    meeting_type: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary_stale(self) -> bool:
        """True when the transcript changed (text or speakers) after it was summarized."""
        if not self.summary or self.summary_data is None or not self.summary_data.source_hash:
            return False
        return self.summary_data.source_hash != transcript_hash(self.transcript)


class SessionSummary(ApiModel):
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
    local_only: bool = False
