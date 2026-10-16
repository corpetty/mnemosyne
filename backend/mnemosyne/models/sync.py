"""Library sync between this computer and the ones paired with it (services/sync_peers.py)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .base import ApiModel
from .sync_session import SyncSession, SyncTombstone


class SyncSelf(ApiModel):
    id: str  # this machine's stable sync id
    name: str


class SyncPeerInfo(ApiModel):
    """A paired machine. Never the token this machine holds for it."""

    id: str  # the other machine's sync id
    name: str
    endpoint_id: str
    created_at: datetime
    last_sync_at: datetime | None
    listen: str | None  # where this machine reaches it: a loopback address of the link sidecar


class SyncPeers(ApiModel):
    self: SyncSelf
    peers: list[SyncPeerInfo]


# What syncing with a peer is doing: idle (not tried since this start), syncing, done (the
# last round finished), offline (not reachable; tried again with a backoff), locked (its
# meetings are encrypted and locked), skew (another sync protocol: update one of the two),
# error (the last round failed; `error` says why).
SyncState = Literal["idle", "syncing", "done", "offline", "locked", "skew", "error"]


class SyncClash(ApiModel):
    """A meeting with the same id on both computers that is not the same meeting (it started
    at another time): never merged, left as it is on each."""

    session_id: str
    name: str


class SyncPeerStatus(ApiModel):
    id: str
    name: str
    endpoint: str  # the first 10 characters of its endpoint id
    listen: str | None
    last_sync_at: datetime | None
    state: SyncState
    error: str | None  # the last round's problem, for the person
    done: int  # meetings handled in the current (or last) round
    total: int  # meetings the peer had changed when the round started
    received: int  # new meetings in the last round
    updated: int  # meetings taken over as they were there (fast-forward)
    merged: int  # meetings edited on both computers and merged
    deleted: int  # meetings deleted here because they were deleted there
    clashes: list[SyncClash]
    app_version: str | None  # the peer's Mnemosyne version, once it answered


class SyncHello(ApiModel):
    """Who a computer is and which sync it speaks (GET /api/sync/hello)."""

    protocol: int
    schema_version: int
    app_version: str
    id: str
    name: str
    seq: int  # its change counter now (a cursor past it means its log started over)


class SyncChanges(ApiModel):
    """A page of a computer's changed meetings (GET /api/sync/changes): whole documents, oldest
    change first, and the meetings deleted since the tombstone cursor."""

    sessions: list[SyncSession]
    tombstones: list[SyncTombstone]
    next_since: int  # ask from here for the next page
    more: bool  # more changes after this page
    total: int  # meetings changed after `since` (for progress)


class SyncPokeResponse(ApiModel):
    scheduled: bool  # a pull from the caller is coming (false: the caller is not a peer)


class SyncNowResponse(ApiModel):
    started: list[str] = Field(default_factory=list)  # ids of the peers being pulled from


class SyncStatus(ApiModel):
    self: SyncSelf
    peers: list[SyncPeerStatus]


class SyncPairRequest(BaseModel):
    invite: str  # <ticket>#<code>, from "New sync invite" on the other computer
    name: str = ""  # what to call this computer (kept as its sync name)


class SyncPairResponse(ApiModel):
    peer: SyncPeerInfo


class SyncPeerPayload(BaseModel):
    """What a computer pairing for sync tells the other about itself (the sidecar passes it
    through unchanged: link/src/lib.rs PeerInfo)."""

    id: str = ""
    name: str = ""
    ticket: str = ""
    token: str = ""
