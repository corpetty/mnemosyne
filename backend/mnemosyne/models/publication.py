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
    # The people to list as attendees; anyone of the meeting not listed here is also taken out
    # of every text, as "Person N".
    attendees: list[str] = Field(default_factory=list)
    names: NameMode = "as_is"  # as they are, first names only, or Person 1, Person 2...
    hide_identifiers: bool = True  # SSN, account, card numbers... (privacy.redact_identifiers)
    quotes: list[QuoteSpan] = Field(default_factory=list)
    where: Where = "local"  # "local": served by this computer; "host": the publish host
    expires_days: int | None = 90  # None: never (only for "local")


class PublicationDefaults(PublicationSelection):
    """A selection to start the Publish dialog from (send it back as a selection), and who
    could open a link to this computer."""

    local_reach: LocalReach
    local_url: str  # the base such a link would use; "" for this_device


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


class PublicationListItem(ApiModel):
    publication: Publication
    session_name: str
