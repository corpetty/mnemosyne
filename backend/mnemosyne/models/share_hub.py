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
