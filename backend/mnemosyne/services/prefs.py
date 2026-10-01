"""Per-person preferences on a team server: the few settings that are about a person, not the
server. Each is optional; unset means the server's setting. Stored in the database (`user_prefs`),
so they are encrypted at rest with the meetings.

Work on a meeting uses its owner's preferences (summary style and instructions, mention alerts,
the HubSpot owner, the name on the microphone channel); views of your own use yours (calendar).
On the desktop app (nobody signed in) there are none: `effective` is the server's settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from ..models.base import ApiModel

if TYPE_CHECKING:
    from ..api.context import AppContext
    from ..config import Settings
    from ..models.session import Session


class UserPrefs(ApiModel):
    summary_style: str | None = None
    summary_instructions: str | None = None
    mention_keywords: str | None = None
    calendar_ics_url: str | None = None
    hubspot_owner_email: str | None = None
    # Share the meetings you create: "private" (only you, and whom you share them with) or "team".
    share_new_meetings: Literal["private", "team"] | None = None
    # Your weekly digest (services/digest_service.py): weekday 0-6, -1 off; at or after the hour.
    digest_weekday: int | None = Field(default=None, ge=-1, le=6)
    digest_hour: int | None = Field(default=None, ge=0, le=23)


# Preferences that override the server's setting of the same name.
SETTINGS_FIELDS = (
    "summary_style",
    "summary_instructions",
    "mention_keywords",
    "calendar_ics_url",
    "hubspot_owner_email",
    "digest_weekday",
    "digest_hour",
)


def get(app: AppContext, user_id: str) -> UserPrefs:
    return UserPrefs.model_validate(app.repo.user_prefs(user_id)) if user_id else UserPrefs()


def save(app: AppContext, user_id: str, prefs: UserPrefs) -> UserPrefs:
    app.repo.set_user_prefs(user_id, prefs.model_dump(exclude_none=True))
    return get(app, user_id)


def effective(app: AppContext, user_id: str) -> Settings:
    """The server's settings with this person's preferences on top. On a team server the
    microphone channel is theirs, so it carries their name."""
    settings = app.settings
    if not (settings.team_mode and user_id):
        return settings
    mine = get(app, user_id)
    update = {k: getattr(mine, k) for k in SETTINGS_FIELDS if getattr(mine, k) is not None}
    if user := app.users.get(user_id):
        update["local_speaker_name"] = user.name
    return settings.model_copy(update=update) if update else settings


def for_meeting(app: AppContext, session: Session | None) -> Settings:
    """Settings for work on a meeting: its owner's."""
    return effective(app, session.owner_id if session is not None else "")


def share_if_wanted(app: AppContext, session_id: str, owner_id: str) -> None:
    """Share a new meeting with everyone when its owner's preference says so."""
    if not (app.settings.team_mode and owner_id):
        return
    if get(app, owner_id).share_new_meetings == "team":
        from .sharing import EVERYONE, add_shares

        add_shares(app, session_id, {EVERYONE}, "their preference")
