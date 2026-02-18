"""Session data model with JSON persistence."""

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from .transcript import TranscriptSegment


class SessionStatus(str, Enum):
    CREATED = "created"
    RECORDING = "recording"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"


class Session(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    name: str = "Untitled Session"
    status: SessionStatus = SessionStatus.CREATED
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    audio_file: str | None = None
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    summary: str = ""
    notes: str = ""
    participants: list[str] = Field(default_factory=list)

    def save(self, data_dir: Path) -> Path:
        """Persist session to a JSON file."""
        sessions_dir = data_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / f"{self.id}.json"
        self.updated_at = datetime.now()
        path.write_text(self.model_dump_json(indent=2))
        return path

    @classmethod
    def load(cls, path: Path) -> "Session":
        """Load a session from a JSON file."""
        data = json.loads(path.read_text())
        return cls.model_validate(data)
