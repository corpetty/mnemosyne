"""A meeting as one contact receives it (phase 8; services/shares.py builds it).

The projection of one share: only the parts the owner chose, signed with the owner's person
key. Nothing outside the chosen parts is serialized, and some things have no field at all:
audio, recordings and their paths, speaker embeddings and voice data, publications (keys
included), tokens, history, seals, supervision and the parts of other people's shares. A test
walks this model's JSON schema to keep it so (tests/test_shared_meeting.py).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .base import ApiModel
from .session import SummaryData
from .sync_session import SyncComment


class ShareParts(ApiModel):
    """What of a meeting a share includes (the stored `peer_shares.parts`, and the sharing
    dialog's request). The title and date always travel. The transcript is off by default: it
    cannot be taken back once sent. `hide_identifiers` redacts account, card and social security
    numbers, dates of birth, e-mail addresses and phone numbers in the summary and the
    transcript (not in the notes or comments)."""

    summary: bool = True
    decisions: bool = True
    questions: bool = True
    action_items: bool = True
    chapters: bool = True
    topics: bool = True
    attendees: bool = True
    notes: bool = True
    transcript: bool = False
    hide_identifiers: bool = False

    @classmethod
    def from_stored(cls, stored: Any) -> "ShareParts":
        """Read a stored `parts` dict leniently: unknown keys are ignored (a newer version may
        have added some), a missing or non-boolean value takes its default."""
        if not isinstance(stored, dict):
            return cls()
        known = {
            name: value for name in cls.model_fields if isinstance(value := stored.get(name), bool)
        }
        return cls(**known)


class SharedOwner(ApiModel):
    person: str  # the owner's public key, 64 hex characters
    name: str  # what the owner is called to others


class SharedSegment(ApiModel):
    """A transcript line on the meeting's joined timeline (no words, no audio references)."""

    speaker: str
    text: str
    start: float
    end: float


class SharedMeeting(ApiModel):
    format: int = 1
    id: str
    name: str
    created_at: datetime  # the meeting's
    updated_at: datetime
    owner: SharedOwner
    parts: ShareParts
    summary: str = ""
    summary_data: SummaryData | None = None
    attendees: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    transcript: list[SharedSegment] = Field(default_factory=list)
    notes: str = ""  # markdown, as folded
    notes_doc: str = ""  # base64 of the notes document's snapshot ("" = none)
    comments: list[SyncComment] = Field(default_factory=list)
    # Everyone with a live share on this meeting, the recipient included (display names).
    shared_with: list[str] = Field(default_factory=list)
    digest: str = ""  # sha256 hex of the canonical JSON of everything but `digest` and `sig`
    sig: str = ""  # the owner's signature (hex) of the canonical JSON of everything but `sig`


# ---- what a recipient sends back (POST /api/share/contributions; services/shares.py) -------

CONTRIBUTION_KINDS = ("comment", "item_flag", "notes_frames")


class Contribution(ApiModel):
    """One signed piece of work by a recipient on a meeting shared with them.

    `sig` is the author's Ed25519 signature (hex) of `PersonKeys.canonical` of this record's
    JSON without `sig` (`model_dump(mode="json", exclude={"sig"})`: id, kind, session_id,
    payload, author, at). The payload by kind:

    - comment: `{op: "create"|"edit"|"delete"|"resolve"|"reopen", comment_id, ...}`; create
      adds the CommentCreate fields (target, target_id, parent_id, text, idx, idx_to, seconds,
      exact, prefix, suffix, notes_ids) and `comment_id` is minted by the sender (12 hex);
      edit adds `text`; the others nothing.
    - item_flag: `{item_id, flag: "done"|"follow", value: bool}`.
    - notes_frames: `{frame_hashes: [sha256 hex, ...]}` in order; the frames travel beside the
      records in `ContributionBatch.frames`, so the signature covers them by hash.
    """

    id: str = Field(pattern=r"^[0-9a-f]{12}$")
    kind: Literal["comment", "item_flag", "notes_frames"]
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    author: str = Field(pattern=r"^[0-9a-f]{64}$")  # the author's person key
    at: str  # when it was done, ISO 8601 on the author's clock (kept as sent: it is signed)
    sig: str


class ContributionBatch(ApiModel):
    """Records in the order they were made (FIFO per meeting), and the notes frames they list:
    sha256 hex of the frame's bytes -> the bytes in base64."""

    records: list[Contribution] = Field(default_factory=list)
    frames: dict[str, str] = Field(default_factory=dict)


class ContributionResult(ApiModel):
    id: str
    ok: bool
    reason: str = ""  # why it was refused, readable ("" when ok)


class ContributionResponse(ApiModel):
    results: list[ContributionResult] = Field(default_factory=list)
