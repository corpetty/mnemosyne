"""Meetings someone shared with me, on my computer (phase 8, docs/plans/2026-10-23-phase-8-tasks.md
C6, C7, C9): a `sessions` row with `shared_from` = the owner's person key.

The desktop runs with no Principal (unrestricted), so the writers that do not go through the
repository's `_check_write` follow the share's rules here, the same ones the owner's hub applies
when it takes the contribution (services/shares.py `accept_contributions`): comments run as me
(`contacts.my_principal`), ticks need the action items shared, typing needs the notes shared.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .. import access
from ..models.shared_meeting import ShareParts

if TYPE_CHECKING:
    from ..api.context import AppContext

# Which part a comment's target belongs to ('' = the meeting as a whole: always included), as on
# the hub (services/shares.py `_COMMENT_PART`).
COMMENT_PART = {
    "segment": "transcript",
    "notes": "notes",
    "body": "summary",
    "decision": "decisions",
    "question": "questions",
    "item": "action_items",
}

# What a refusal says about each part (as the hub says it, services/shares.py `_PART_LABEL`).
PART_LABEL = {
    "summary": "summary is",
    "transcript": "transcript is",
    "notes": "notes are",
    "decisions": "decisions are",
    "questions": "open questions are",
    "action_items": "action items are",
}


def shared_from(app: AppContext, session_id: str) -> str:
    """The owner's person key when the meeting was shared with me, else ""."""
    return app.repo.shared_from(session_id)


def _stored_parts(app: AppContext, session_id: str) -> Any:
    """The parts the owner shared, as stored with the pulled meeting (`shared_meetings`), or
    None when that is not known (yet)."""
    getter = getattr(app.repo, "shared_meeting", None)
    if not callable(getter):
        return None
    row = getter(session_id)
    if row is None:
        return None
    parts = row.get("parts") if isinstance(row, dict) else getattr(row, "parts", None)
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
        except ValueError:
            return None
    if isinstance(parts, ShareParts):
        return parts.model_dump()
    return parts


def parts_of(app: AppContext, session_id: str, owner: str | None = None) -> ShareParts | None:
    """What of this meeting was shared with me, or None when it is not shared with me (my own
    meeting, or one of my other computers'). `owner`: its `shared_from` when the caller has it.
    Without a stored share, the defaults (ShareParts())."""
    if owner is None:
        owner = shared_from(app, session_id)
    if not owner:
        return None
    stored = _stored_parts(app, session_id)
    return ShareParts() if stored is None else ShareParts.from_stored(stored)


def require_part(parts: ShareParts | None, part: str | None) -> None:
    """Forbidden unless the share includes `part` (None: the meeting as a whole, or a meeting
    not shared with me)."""
    if parts is not None and part is not None and not getattr(parts, part):
        raise access.Forbidden(f"This meeting's {PART_LABEL[part]} not shared with you")


def acting(app: AppContext, session: Any) -> access.Principal | None:
    """Who acts on this meeting: the signed-in person, or on a meeting shared with me (and
    nobody signed in, the desktop) me as the owner's hub knows me."""
    p = access.principal()
    if p is None and getattr(session, "shared_from", ""):
        from .contacts import my_principal

        return my_principal(app)
    return p


def shared_ids(repo) -> set[str]:
    """Every meeting shared with me (that the caller can read)."""
    return {s.id for s in repo.list_summaries() if s.shared_from}


def excluded(app: AppContext, setting: str) -> set[str]:
    """The meetings shared with me to leave out of a feature whose `shared_in_*` setting is
    off (Ask, the semantic index, the digest, Obsidian): empty when it is on."""
    if getattr(app.settings, setting, False):
        return set()
    return shared_ids(app.repo)
