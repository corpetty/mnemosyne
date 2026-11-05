"""Publishing a meeting (services/publishing.py): what to publish, and what was published."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import ApiModel

NameMode = Literal["as_is", "first", "hidden"]
Where = Literal["local", "host"]
# Who can open a link to this computer (services/publishing.py local_reach): "team" (the team
# listener is on), "phone_url" (a configured address), "this_device" (127.0.0.1 only: nobody else).
LocalReach = Literal["team", "phone_url", "this_device"]
# A hosted publication's live channel (services/live_publications.py): "live" (the owner's
# bridge is connected to the host's room), "offline" (not running or cannot reach the host),
# "unsupported" (the host or the place cannot do it; `live_reason` says why), "off" (neither
# `follow` nor `comments` is on).
LiveState = Literal["live", "offline", "unsupported", "off"]


class QuoteSpan(ApiModel):
    """A stretch of the meeting to quote: the transcript lines whose middle falls inside it,
    and, with `with_clip`, that stretch of audio. Seconds on the meeting's joined timeline, or,
    when `part` is set, seconds into that part (like bookmarks and comments)."""

    start: float
    end: float
    part: int | None = None
    with_clip: bool = False


class PublicationSelection(ApiModel):
    """What a publication shows. The transcript is never published: only quotes' lines."""

    title: str = ""  # the published title ("" = the meeting's name)
    summary: bool = True
    decisions: bool = True
    questions: bool = True
    action_items: bool = True
    chapters: bool = True
    topics: bool = True
    notes: bool = False  # the meeting's (live shared) notes
    # The notes' images to include, by their plain hash (`NotesImage.sha256`): None = all of
    # them, [] = none. A left-out image shows as "[image: caption]". Only with `notes`.
    images: list[str] | None = None
    # The people to list as attendees; anyone of the meeting not listed here is also taken out
    # of every text, as "Person N".
    attendees: list[str] = Field(default_factory=list)
    names: NameMode = "as_is"  # as they are, first names only, or Person 1, Person 2...
    hide_identifiers: bool = True  # SSN, account, card numbers... (privacy.redact_identifiers)
    quotes: list[QuoteSpan] = Field(default_factory=list)
    where: Where = "local"  # "local": served by this computer; "host": the publish host
    expires_days: int | None = 90  # None: never (only for "local")
    # Live publications (docs/plans/2026-11-05-live-publications.md; both need a publish host):
    # `follow` publishes a new version by itself when the meeting changes, with this same
    # selection (with it on, `images` None is frozen into the list published); `comments` lets
    # readers of the link comment.
    follow: bool = False
    comments: bool = False
    # Approve before other readers see (task 13b): while `comments` is on too, a comment a reader
    # writes (and every reply of a reader) reaches the other readers only once the owner has
    # approved it; its writer sees it at once, marked as waiting. `moderated_since` (UTC ISO) is
    # set by the server when the switch goes on: comments made before it count as approved. The
    # owner's Hide keeps a comment from readers whatever `moderate` says.
    moderate: bool = False
    moderated_since: str | None = None


class NotesImage(ApiModel):
    """An image of the meeting's notes, for the Publish dialog's list."""

    sha256: str  # the plain hash: what `PublicationSelection.images` names
    caption: str
    width: int
    height: int
    size: int  # bytes as stored


class PublicationDefaults(PublicationSelection):
    """A selection to start the Publish dialog from (send it back as a selection), and who
    could open a link to this computer."""

    local_reach: LocalReach
    local_url: str  # the base such a link would use; "" for this_device
    # The publish host a "host" publication would go to ("" none: Mnemosyne's servers are off
    # and none is set) and whether it is Mnemosyne's own (services/ours.py).
    host_url: str
    host_is_ours: bool
    # The images of the meeting's notes, in order, each once (`images`, inherited, is which to
    # include: null = all).
    notes_images: list[NotesImage] = Field(default_factory=list)


class Publication(ApiModel):
    id: str  # 16 hex, never the meeting's id
    session_id: str
    where: Where
    host_url: str  # the publish host it is on ("" for this computer)
    # The link with its key in the fragment: only for the meeting's owner or an admin, else null.
    url: str | None
    version: int
    selection: PublicationSelection
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    unpublished_at: datetime | None
    behind: bool  # the meeting changed after the last publish (an update would differ)
    published_by: str
    local_reach: LocalReach  # who could open a link to this computer (for `where` = "local")
    # A "this computer" publication made on another of the person's computers (library sync):
    # that computer's name. It serves the link; here it can be neither updated nor unpublished,
    # and there is no link to show. Null for hosted ones and for those served here.
    served_by: str | None = None
    # The live channel (never the owner keys, which stay on the row). `readers`: the room's
    # connections beside the owner's bridge, 0 when unknown.
    live: LiveState = "off"
    live_reason: str = ""
    readers: int = 0


class PublicationListItem(ApiModel):
    publication: Publication
    session_name: str
