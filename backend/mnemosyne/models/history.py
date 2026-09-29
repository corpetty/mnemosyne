"""A meeting's history: what it is made of and what happened to it (services/history.py)."""

from datetime import datetime
from typing import Any, Literal

from .base import ApiModel


class HistoryEvent(ApiModel):
    at: datetime
    kind: str  # created, recording_started, recording_stopped, part_saved, save_failed, ...
    part: int | None = None
    detail: dict[str, Any] = {}


class HistoryFile(ApiModel):
    name: str  # file name in the meeting's folder
    source: str = ""  # mic | system | import, when known
    device_name: str = ""
    size: int | None = None  # bytes; None when the file is not there
    seconds: float | None = None  # for WAVs waiting to be saved


class HistoryPart(ApiModel):
    part: int
    offset: float  # where it starts on the meeting's timeline (seconds)
    seconds: float | None  # its length, when known
    started_at: datetime | None  # wall clock
    ended_at: datetime | None
    approximate: bool  # times worked out from when the part was saved, not logged
    gap_before: float | None  # seconds not recorded between the previous part and this one
    how: Literal["recorded", "recovered", "imported", "combined", "recording"]
    files: list[HistoryFile]


class PendingRecording(ApiModel):
    """Audio recorded but not in the meeting yet: an interrupted recording, or one whose save
    failed. Recovering it adds it as a part."""

    recording_id: str | None
    part: int | None
    seconds: float
    state: Literal["waiting", "saving"]
    files: list[HistoryFile]


class SessionHistory(ApiModel):
    session_id: str
    recording_now: bool
    parts: list[HistoryPart]
    recorded_seconds: float  # the parts' total
    gap_seconds: float  # between parts, not recorded
    pending: list[PendingRecording]
    missing: list[str]  # files the meeting names that are not on disk
    orphans: list[HistoryFile]  # audio in the meeting's folder that nothing refers to
    events: list[HistoryEvent]
