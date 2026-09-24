"""Weekly (or any date range) digest across meetings."""

from datetime import date, datetime
from uuid import uuid4

from pydantic import Field

from .base import ApiModel


class Digest(ApiModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    label: str  # "2026-W39" for an ISO week, otherwise "2026-09-01 to 2026-09-07"
    start: date
    end: date  # inclusive
    markdown: str = ""
    session_ids: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    path: str | None = None  # where it was written in the Obsidian vault
    created_at: datetime = Field(default_factory=datetime.now)
