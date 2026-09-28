"""Kinds of meeting (a standup, a 1:1, a customer call), each with its own summary style and
instructions, Obsidian folder, local-only and auto-record. A meeting gets its type from its
title when it is named (the calendar, a rename, the summary's title), unless one was chosen;
"none" means the user chose no type.
"""

from __future__ import annotations

from ..config import MeetingType, Settings
from ..models.session import Session

NONE = "none"


def match_type(title: str, types: list[MeetingType]) -> MeetingType | None:
    """The first type with a word or phrase that the title contains (any case)."""
    t = title.lower()
    for kind in types:
        words = [w.strip().lower() for w in kind.match.split(",") if w.strip()]
        if any(w in t for w in words):
            return kind
    return None


def session_type(settings: Settings, session: Session) -> MeetingType | None:
    """The type a meeting has (chosen, or given by its title when it was named)."""
    if not session.meeting_type or session.meeting_type == NONE:
        return None
    wanted = session.meeting_type.lower()
    return next((t for t in settings.meeting_types if t.name.lower() == wanted), None)


def summary_style(settings: Settings, session: Session) -> str:
    kind = session_type(settings, session)
    return (kind.summary_style if kind else "") or settings.summary_style


def summary_instructions(settings: Settings, session: Session) -> str:
    kind = session_type(settings, session)
    extra = kind.instructions if kind else ""
    return f"{settings.summary_instructions}\n{extra}".strip()


def obsidian_folder(settings: Settings, session: Session) -> str:
    kind = session_type(settings, session)
    return (kind.obsidian_folder if kind else "") or settings.obsidian_subfolder
