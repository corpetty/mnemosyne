"""Sharing a meeting on a team server (access.py): its owner (or an admin) lets chosen people, or
everyone ("*"), read it. Sharing gives read access only; the one change a reader may make is
ticking an action item done (routes/tasks.py).

Meetings are also shared on their own: with team members whose email is among the calendar
invitees (`share_with_invitees`), and, for someone whose preference says so, with everyone as
soon as they create it (`share_new_meetings`, services/prefs.py).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import access
from . import history

if TYPE_CHECKING:
    from ..api.context import AppContext

logger = logging.getLogger(__name__)

EVERYONE = "*"


def set_shares(app: AppContext, session_id: str, user_ids: set[str], by: str = "") -> None:
    """Share with exactly these people (user ids, EVERYONE), never with the owner, logged."""
    owner = app.repo.owner_of(session_id) or ""
    known = {u.id for u in app.users.list()}
    wanted = {u for u in user_ids if (u == EVERYONE or u in known) and u != owner}
    who = by or (p.name if (p := access.principal()) else "")
    added, removed = app.repo.set_shares(session_id, wanted, who)
    if not (added or removed):
        return
    names = {u.id: u.name for u in app.users.list()} | {EVERYONE: "everyone"}
    if added:
        history.log(app, session_id, "shared", people=sorted(names[u] for u in added), by=who)
    if removed:
        history.log(app, session_id, "unshared", people=sorted(names[u] for u in removed), by=who)
    # Without the id: someone it was just taken away from may no longer hear about the meeting.
    app.bus.publish({"type": "shares"})


def add_shares(app: AppContext, session_id: str, user_ids: set[str], by: str) -> None:
    current = {s["user_id"] for s in app.repo.shares(session_id)}
    if user_ids - current:
        set_shares(app, session_id, current | user_ids, by)


def share_with_invitees(app: AppContext, session_id: str) -> None:
    """Share with team members invited to the meeting (by email). Never raises."""
    if not (app.settings.team_mode and app.settings.share_with_invitees):
        return
    try:
        session = app.repo.get(session_id)
        if session is None:
            return
        emails = {a.strip().lower() for a in session.attendees if "@" in a}
        emails |= {e.strip().lower() for e in app.repo.attendee_emails(session_id).values() if e}
        invited = {u.id for u in app.users.list() if u.email and u.email.lower() in emails}
        if invited:
            add_shares(app, session_id, invited, "calendar invite")
    except Exception:
        logger.warning("Could not share %s with its invitees", session_id, exc_info=True)
