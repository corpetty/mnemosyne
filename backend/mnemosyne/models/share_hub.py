"""What the hub side of sharing answers (api/routes/share.py) and the owner's share management
(api/routes/peer_shares.py); docs/plans/2026-10-23-phase-8-tasks.md."""

from pydantic import Field

from .base import ApiModel
from .shared_meeting import SharedMeeting, ShareParts

SHARE_PROTOCOL = 1


class ShareHello(ApiModel):
    protocol: int
    app_version: str
    person: str  # this person's public key, 64 hex characters
    name: str


class ShareTombstone(ApiModel):
    session_id: str
    at: str  # this computer's local time, ISO 8601
    reason: str  # "unshared" or "deleted"


class ShareChanges(ApiModel):
    """A page of what changed for the calling contact: whole signed meetings, oldest change
    first, and what it must forget. Ask again from `next_since` while `more`;
    `tombstones_next` is the `tombstones_since` for the next round."""

    sessions: list[SharedMeeting] = Field(default_factory=list)
    next_since: int
    more: bool
    tombstones: list[ShareTombstone] = Field(default_factory=list)
    tombstones_next: str


class PeerShareInfo(ApiModel):
    """A live share of a meeting with a contact."""

    person: str
    name: str
    parts: ShareParts
    at: str  # when it was first shared
    updated_at: str


class ShareOwner(ApiModel):
    """Who shared a meeting with this person."""

    person: str
    name: str


class ShareWaiting(ApiModel):
    """What this person did on the meeting that its owner's computer has not taken yet."""

    comments: int
    ticks: int
    notes: bool


class ShareRejected(ApiModel):
    """Something the owner's computer refused, and why."""

    kind: str  # comment | item_flag | notes
    reason: str
    excerpt: str  # the first 80 characters of the comment or the action item ("" for notes)
    at: str  # when it was refused, ISO 8601 UTC ("" for notes)


class ShareStatus(ApiModel):
    """How this person's work on a meeting shared with them stands (services/share_push.py)."""

    owner: ShareOwner
    parts: ShareParts  # what the owner shared with me: what I may see and do
    waiting: ShareWaiting
    rejected: list[ShareRejected] = Field(default_factory=list)
