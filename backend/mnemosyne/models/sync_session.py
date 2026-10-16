"""A meeting as one portable document for library sync (services/sync_state.py).

Everything that travels between a person's machines for one meeting, per
docs/plans/2026-10-14-library-sync-rules.md section 2. Not in it: `status` and `audio_file`
(per machine), attendee emails, legal hold, CRM state, summary documents, vault notes,
versions and seals (local bookkeeping), audio and keys.
"""

from datetime import datetime

from pydantic import Field

from .base import ApiModel
from .live_flag import LiveFlag
from .session import (
    AgendaItem,
    CopilotNotes,
    ExternalNotes,
    Recording,
    SummaryData,
)
from .transcript import TranscriptSegment

# History events that are machine-local happenings and never leave the machine: the access log
# (viewed, played, exported), recovery and capture trouble, CRM pushes.
NEVER_SENT_EVENTS = frozenset(
    {"viewed", "played", "exported", "interrupted", "capture_problem", "capture_ok"}
)
NEVER_SENT_PREFIXES = ("recover_", "hubspot_")


def event_travels(kind: str) -> bool:
    """Whether a history event of this kind is part of the meeting that syncs."""
    return kind not in NEVER_SENT_EVENTS and not kind.startswith(NEVER_SENT_PREFIXES)


class SyncBookmark(ApiModel):
    """A bookmark as stored: (part, seconds into it), not a place on the joined timeline."""

    id: str
    part: int = 0
    seconds: float = 0.0
    note: str = ""
    created_at: datetime


class SyncAsset(ApiModel):
    """An asset a meeting uses. Links and the text read from files travel; file bytes never do
    (`filename` and `size` only say what the file is). `added_at`: when it was attached to
    this meeting."""

    id: str
    kind: str  # "link" | "file"
    title: str
    url: str | None = None
    filename: str | None = None
    size: int | None = None
    source: str = "manual"
    text: str | None = None
    created_at: datetime
    added_at: datetime


class SyncEvent(ApiModel):
    """A history event. `uid` is its identity across machines; `at` stays the stored ISO text."""

    uid: str
    at: str
    kind: str
    part: int | None = None
    detail: dict = Field(default_factory=dict)


class SyncSession(ApiModel):
    id: str
    created_at: datetime
    updated_at: datetime
    name: str = ""
    summary: str = ""
    summary_data: SummaryData | None = None
    notes: str = ""
    participants: list[str] = Field(default_factory=list)
    attendees: list[str] = Field(default_factory=list)
    local_only: bool = False
    copilot_notes: CopilotNotes | None = None
    speakers_reviewed: bool = False
    agenda: list[AgendaItem] = Field(default_factory=list)
    meeting_type: str = ""
    owner_id: str = ""
    # The origin's `audio_file`, for display only: never applied.
    origin_audio: str | None = None
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    recordings: list[Recording] = Field(default_factory=list)  # rows as the origin has them
    bookmarks: list[SyncBookmark] = Field(default_factory=list)
    external_notes: list[ExternalNotes] = Field(default_factory=list)
    live_flags: list[LiveFlag] = Field(default_factory=list)
    speaker_embeddings: dict[str, list[float]] = Field(default_factory=dict)
    assets: list[SyncAsset] = Field(default_factory=list)
    events: list[SyncEvent] = Field(default_factory=list)
    # The recording machine's sync id as the SENDER sees it (its own device id when the meeting
    # is its own), and the sender's version vector.
    origin: str
    vv: dict[str, int] = Field(default_factory=dict)


class SyncTombstone(ApiModel):
    """A meeting deleted: where, when (ISO text, as stored) and what it was called."""

    session_id: str
    at: str
    device: str
    name: str = ""
