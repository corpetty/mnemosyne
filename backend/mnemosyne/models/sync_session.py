"""A meeting as one portable document for library sync (services/sync_state.py).

Everything that travels between a person's machines for one meeting, per
docs/plans/2026-10-14-library-sync-rules.md section 2. Not in it: `status` and `audio_file`
(per machine), attendee emails, legal hold, CRM state, summary documents, vault notes,
versions and seals (local bookkeeping), audio and encryption keys. Publications travel with
their link key and token (see `SyncPublication`).
"""

import json
from datetime import UTC, datetime

from pydantic import Field

from .base import ApiModel
from .live_flag import LiveFlag
from .publication import PublicationSelection, Where
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


class SyncComment(ApiModel):
    """A comment as stored (models/comment.py). Timestamps stay the stored ISO text; the
    computed read fields (`changed`, `detached`, ...) are not part of it."""

    id: str
    parent_id: str = ""
    target: str = ""
    target_id: str = ""
    anchor: dict = Field(default_factory=dict)
    quote: str = ""
    text: str = ""
    author_id: str = ""
    author: str = ""
    mentions: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    resolved_at: str | None = None
    resolved_by: str = ""
    deleted: bool = False


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


class SyncPublication(ApiModel):
    """A publication row as stored (services/publishing.py), key and token included: library
    sync is between one person's own computers, which must be able to update a link. Times stay
    the stored ISO text. `origin`: the computer whose files serve a "this computer"
    publication, as the SENDER sees it (its own sync id when it is its own)."""

    id: str
    key: str  # base64 of the 32-byte key
    token: str
    where: Where
    host_url: str = ""
    version: int
    selection: PublicationSelection
    created_at: str
    updated_at: str
    expires_at: str | None = None
    unpublished_at: str | None = None
    clip_blobs: dict[str, dict] = Field(default_factory=dict)
    published_digest: str = ""
    published_by: str = ""
    origin: str = ""


def _when(text: str) -> datetime:
    at = datetime.fromisoformat(text)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _rank(p: SyncPublication) -> tuple:
    """Which of two states of one publication is the later: the newer `updated_at`; at equal
    times an unpublished one beats a live one (a deletion must not be undone by a tie), then
    the higher version."""
    return (_when(p.updated_at), bool(p.unpublished_at), p.version)


def merge_publications(
    ours: list[SyncPublication], theirs: list[SyncPublication]
) -> list[SyncPublication]:
    """The publications of a meeting after two machines' states meet. By id, the row with the
    newer `updated_at` wins whole, except that the version is the larger of the two (it never
    goes back). A meeting has one live publication: when two machines published separately
    (two ids, both live), the later one stays and the other becomes unpublished, as of the
    later time, so that state travels back as an ordinary unpublish. Ours first, in order;
    theirs that we lack after."""
    by_id: dict[str, SyncPublication] = {}
    for p in [*ours, *theirs]:
        mine = by_id.get(p.id)
        if mine is None:
            by_id[p.id] = p
            continue
        win = p if _rank(p) > _rank(mine) else mine
        by_id[p.id] = win.model_copy(update={"version": max(mine.version, p.version)})
    live = [p for p in by_id.values() if not p.unpublished_at]
    if len(live) > 1:
        keep = max(live, key=lambda p: (_rank(p), p.id))
        at = max(live, key=lambda p: _when(p.updated_at)).updated_at
        for p in live:
            if p is not keep:
                by_id[p.id] = p.model_copy(update={"unpublished_at": at, "updated_at": at})
    return list(by_id.values())


class SyncPeerShare(ApiModel):
    """A meeting shared with a contact (a `peer_shares` row): `person` is the contact's key,
    `parts` what was chosen (an opaque dict here). Times stay the stored ISO text. A revoked
    share keeps its row (`revoked_at`), so the revocation travels."""

    person: str
    parts: dict = Field(default_factory=dict)
    by: str = ""
    at: str
    updated_at: str
    revoked_at: str = ""


def peer_share_rank(p: SyncPeerShare) -> tuple:
    """Which of two states of one share is the later: the newer `updated_at`; at equal times a
    revoked one beats a live one (a revocation must not be undone by a tie). The rest only
    makes the choice the same on both machines."""
    return (
        _when(p.updated_at),
        bool(p.revoked_at),
        p.revoked_at,
        p.by,
        json.dumps(p.parts, sort_keys=True),
    )


def merge_peer_shares(
    ours: list[SyncPeerShare], theirs: list[SyncPeerShare]
) -> list[SyncPeerShare]:
    """The shares of a meeting after two machines' states meet: by person, the later row wins
    whole (`peer_share_rank`). A side that does not know the field (an older peer) loses
    nothing. Ours first, in order; theirs that we lack after."""
    by_person: dict[str, SyncPeerShare] = {}
    for p in [*ours, *theirs]:
        mine = by_person.get(p.person)
        if mine is None or peer_share_rank(p) > peer_share_rank(mine):
            by_person[p.person] = p
    return list(by_person.values())


class SyncSession(ApiModel):
    id: str
    created_at: datetime
    updated_at: datetime
    name: str = ""
    summary: str = ""
    summary_data: SummaryData | None = None
    notes: str = ""
    # The live shared notes document (services/shared_notes.py): base64 of the canonical
    # snapshot, folded when served; None when the meeting has none (or the sidecar is missing
    # there). Merged by op-set union (services/sync_notes.py), never three-way.
    notes_doc: str | None = None
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
    comments: list[SyncComment] = Field(default_factory=list)  # absent from older peers
    # The meeting's publications (services/publishing.py), normally none or one: merged by id
    # (`merge_publications`), never three-way. Absent from older peers.
    publications: list[SyncPublication] = Field(default_factory=list)
    # Who the meeting is shared with (phase 8): merged by person, newer `updated_at` wins
    # (`merge_peer_shares`). Absent from older peers, which must not wipe ours.
    peer_shares: list[SyncPeerShare] = Field(default_factory=list)
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
