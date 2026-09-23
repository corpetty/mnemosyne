"""Ask-across-meetings models."""

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class PassageLine(BaseModel):
    idx: int
    speaker: str
    start: float
    text: str


class Passage(BaseModel):
    """A contiguous run of transcript lines (or a session summary) retrieved for a question."""

    session_id: str
    session_name: str
    created_at: datetime
    kind: str = "transcript"  # transcript | summary
    lines: list[PassageLine] = Field(default_factory=list)
    text: str = ""  # for kind=summary
    score: float = 0.0
    focus_idx: int | None = None  # the best-matching line inside the window

    def focus_line(self) -> PassageLine | None:
        if not self.lines:
            return None
        return next((ln for ln in self.lines if ln.idx == self.focus_idx), self.lines[0])


class Citation(BaseModel):
    n: int
    session_id: str
    session_name: str
    created_at: datetime
    idx: int | None  # first transcript line of the passage, None for a summary
    start: float | None
    excerpt: str


class Ask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    question: str
    answer: str = ""
    citations: list[Citation] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
