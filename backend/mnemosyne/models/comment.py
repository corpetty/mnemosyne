"""Comments on a meeting: threads on transcript lines, summary blocks and text (storage in
SessionRepository, design in docs/plans/2026-10-12-hashseq-collaboration.md "Phase 5")."""

from datetime import datetime

from pydantic import Field

from .base import ApiModel


class Comment(ApiModel):
    id: str
    session_id: str
    parent_id: str = ""  # '' = the root of a thread, else the root's id
    # What it is on: "segment" (transcript line or moment), "body" (summary text), or a summary
    # block: "decision", "question", "item"; "notes" (the live shared notes); '' = the meeting as
    # a whole.
    target: str = ""
    target_id: str = ""  # the block's id (decision/question/item)
    # Where: {part, start, end} for a transcript span (start == end: a moment of the audio),
    # {exact, prefix, suffix} for summary text, {mark: "comment:<id>"} for the notes (the editor
    # marks the range in the live document; where it is now comes from the last fold).
    # Re-anchored at read time.
    anchor: dict = Field(default_factory=dict)
    quote: str = ""  # what it was written about, kept so it can still be shown once detached
    text: str
    author_id: str = ""
    author: str = ""
    mentions: list[str] = Field(default_factory=list)  # user ids named with @
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str = ""
    deleted: bool = False  # kept as an empty row while replies hang on it
    # Filled when read (services, not stored):
    changed: bool = False  # the text it was written about has changed since
    detached: bool = False  # its anchor is gone
    segment_idx: int | None = None  # the transcript line it is on now
    # Where a transcript comment starts on the meeting's timeline (seconds): "at 12:34" for a
    # moment between lines.
    at_seconds: float | None = None
    # A notes comment's range in the notes' plain text, [start, end) in code points (U+001F cells
    # are tabs), as of the last fold; None when detached (every character of it is gone, or the
    # editor never marked it) or not a notes comment. The live editor finds the mark itself
    # (`anchor.mark`) and uses this when the document is not open.
    notes_range: list[int] | None = None
    can_edit: bool = False
    can_delete: bool = False
    can_resolve: bool = False


class CommentThread(ApiModel):
    root: Comment
    replies: list[Comment] = Field(default_factory=list)


class SessionComments(ApiModel):
    threads: list[CommentThread] = Field(default_factory=list)
    can_comment: bool = False


class CommentCreate(ApiModel):
    target: str = ""
    target_id: str = ""
    parent_id: str = ""
    text: str
    # Anchor inputs: a transcript line (idx, or idx..idx_to), a moment of the audio (seconds),
    # or a selection in the summary text (exact, with prefix/suffix) or the notes (exact: the
    # selected text; the new comment's id names the mark the client then puts on the range).
    idx: int | None = None
    idx_to: int | None = None
    seconds: float | None = None
    exact: str = ""
    prefix: str = ""
    suffix: str = ""
    # A notes comment: the element ids (hex, the editor's `seqIdAt`) of the first and the last
    # selected character. The server puts the mark `comment:<id>` on that range, so a reader
    # (whose editor is read-only) can comment too. Without them, or when an id is unknown or
    # deleted, the thread is kept, detached. An editor may mark the range itself instead.
    notes_ids: list[str] = Field(default_factory=list)


class CommentUpdate(ApiModel):
    text: str


# What a comment is on, for search results and lists ("" = the meeting as a whole).
TARGET_LABELS = {
    "segment": "Transcript",
    "body": "Summary",
    "decision": "Decision",
    "question": "Question",
    "item": "Action item",
    "notes": "Notes",
}
